"""Structured discussion records — roundtable results with summary/resolution.

Goes beyond raw transcript storage: captures summary, resolution,
action items, and status.  Each project gets its own JSON file at
``~/.tunapi/project_memory/{alias}_discussions.json``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import msgspec

from ..logging import get_logger
from ..state_store import JsonStateStore
from .project_memory import generate_entry_id

if TYPE_CHECKING:
    from .roundtable import RoundtableSession

logger = get_logger(__name__)

DiscussionStatus = Literal["open", "resolved", "archived"]

_STATE_VERSION = 1


class ActionItem(msgspec.Struct, forbid_unknown_fields=False):
    id: str
    description: str
    assignee: str | None = None
    done: bool = False


class DiscussionRecord(msgspec.Struct, forbid_unknown_fields=False):
    discussion_id: str
    project_alias: str
    topic: str
    participants: list[str]
    rounds: int
    transcript: list[list[str]]  # [[engine, answer], ...] — JSON-safe
    created_at: float
    status: DiscussionStatus = "open"
    summary: str | None = None
    resolution: str | None = None
    branch_name: str | None = None
    action_items: list[ActionItem] = msgspec.field(default_factory=list)


class _State(msgspec.Struct, forbid_unknown_fields=False):
    version: int = _STATE_VERSION
    records: dict[str, DiscussionRecord] = msgspec.field(default_factory=dict)


def _transcript_to_json(
    transcript: list[tuple[str, str]] | list[list[str]],
) -> list[list[str]]:
    """Normalize transcript to JSON-safe list[list[str]]."""
    return [[str(e), str(a)] for e, a in transcript]


def _transcript_to_tuples(
    transcript: list[list[str]],
) -> list[tuple[str, str]]:
    """Convert stored transcript back to tuple pairs."""
    return [(e, a) for e, a in transcript]


class DiscussionRecordStore:
    """Per-project discussion record store."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._stores: dict[str, JsonStateStore[_State]] = {}

    def _store_for(self, project: str) -> JsonStateStore[_State]:
        store = self._stores.get(project)
        if store is None:
            path = self._base_dir / f"{project}_discussions.json"
            store = cast(
                JsonStateStore[_State],
                JsonStateStore(
                    path,
                    version=_STATE_VERSION,
                    state_type=_State,
                    state_factory=_State,
                    log_prefix="discussion_records",
                    logger=logger,
                ),
            )
            self._stores[project] = store
        return store

    async def create_record(
        self,
        project: str,
        *,
        discussion_id: str | None = None,
        topic: str,
        participants: list[str],
        rounds: int,
        transcript: list[tuple[str, str]] | list[list[str]],
        summary: str | None = None,
        branch_name: str | None = None,
    ) -> DiscussionRecord:
        did = discussion_id or generate_entry_id()
        record = DiscussionRecord(
            discussion_id=did,
            project_alias=project,
            topic=topic,
            participants=participants,
            rounds=rounds,
            transcript=_transcript_to_json(transcript),
            created_at=time.time(),
            summary=summary,
            branch_name=branch_name,
        )
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            store._state.records[did] = record
            store._save_locked()
        return record

    async def get_record(
        self, project: str, discussion_id: str
    ) -> DiscussionRecord | None:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            return store._state.records.get(discussion_id)

    async def list_records(
        self,
        project: str,
        *,
        status: DiscussionStatus | None = None,
    ) -> list[DiscussionRecord]:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            records = list(store._state.records.values())
        if status is not None:
            records = [r for r in records if r.status == status]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    async def update_resolution(
        self, project: str, discussion_id: str, resolution: str
    ) -> DiscussionRecord | None:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            record = store._state.records.get(discussion_id)
            if record is None:
                return None
            record.resolution = resolution
            record.status = "resolved"
            store._save_locked()
            return record

    async def add_action_item(
        self,
        project: str,
        discussion_id: str,
        description: str,
        assignee: str | None = None,
    ) -> ActionItem | None:
        item = ActionItem(
            id=generate_entry_id(),
            description=description,
            assignee=assignee,
        )
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            record = store._state.records.get(discussion_id)
            if record is None:
                return None
            record.action_items.append(item)
            store._save_locked()
        return item

    async def complete_action_item(
        self, project: str, discussion_id: str, action_id: str
    ) -> bool:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            record = store._state.records.get(discussion_id)
            if record is None:
                return False
            for item in record.action_items:
                if item.id == action_id:
                    item.done = True
                    store._save_locked()
                    return True
            return False

    async def archive_record(self, project: str, discussion_id: str) -> bool:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            record = store._state.records.get(discussion_id)
            if record is None:
                return False
            record.status = "archived"
            store._save_locked()
            return True

    @staticmethod
    def from_roundtable_session(
        session: RoundtableSession,
        project_alias: str,
        *,
        summary: str | None = None,
        branch_name: str | None = None,
    ) -> DiscussionRecord:
        """Build a :class:`DiscussionRecord` from a completed roundtable.

        Does NOT persist — caller should pass result to :meth:`create_record`
        or use :meth:`ProjectMemoryFacade.save_roundtable`.
        """
        return DiscussionRecord(
            discussion_id=session.thread_id,
            project_alias=project_alias,
            topic=session.topic,
            participants=session.engines,
            rounds=session.current_round,
            transcript=_transcript_to_json(session.transcript),
            created_at=time.time(),
            summary=summary,
            branch_name=branch_name,
        )
