"""Synthesis artifact — structured output from roundtables / discussions.

A synthesis captures the distilled result of a multi-agent discussion:
thesis, agreements, disagreements, open questions, and action items.
Multiple versions can exist for the same source (e.g. after follow-up
rounds refine the conclusion).

Each project gets its own JSON file at
``~/.tunapi/project_memory/{alias}_synthesis.json``.
"""

from __future__ import annotations

import time
from builtins import list as list_type
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import msgspec

from ..logging import get_logger
from ..state_store import JsonStateStore
from .discussion_records import ActionItem
from .project_memory import generate_entry_id

if TYPE_CHECKING:
    from .discussion_records import DiscussionRecord

logger = get_logger(__name__)

SourceType = Literal["roundtable", "discussion", "manual"]

_STATE_VERSION = 1


SynthesisStatus = Literal["draft", "finalized", "adopted"]


class SynthesisArtifact(msgspec.Struct, forbid_unknown_fields=False):
    artifact_id: str
    source_type: SourceType
    source_id: str  # RT session_id or discussion_id
    version: int
    thesis: str
    created_at: str
    agreements: list[str] = msgspec.field(default_factory=list)
    disagreements: list[str] = msgspec.field(default_factory=list)
    open_questions: list[str] = msgspec.field(default_factory=list)
    action_items: list[ActionItem] = msgspec.field(default_factory=list)
    round_idx: int = 0
    status: SynthesisStatus = "draft"


class _State(msgspec.Struct, forbid_unknown_fields=False):
    version: int = _STATE_VERSION
    artifacts: dict[str, SynthesisArtifact] = msgspec.field(default_factory=dict)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class SynthesisStore:
    """Per-project synthesis artifact store."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._stores: dict[str, JsonStateStore[_State]] = {}

    def _store_for(self, project: str) -> JsonStateStore[_State]:
        store = self._stores.get(project)
        if store is None:
            path = self._base_dir / f"{project}_synthesis.json"
            store = cast(
                JsonStateStore[_State],
                JsonStateStore(
                    path,
                    version=_STATE_VERSION,
                    state_type=_State,
                    state_factory=_State,
                    log_prefix="synthesis",
                    logger=logger,
                ),
            )
            self._stores[project] = store
        return store

    async def create(
        self,
        project: str,
        *,
        source_type: SourceType,
        source_id: str,
        thesis: str,
        agreements: list_type[str] | None = None,
        disagreements: list_type[str] | None = None,
        open_questions: list_type[str] | None = None,
        action_items: list_type[ActionItem] | None = None,
        version: int = 1,
    ) -> SynthesisArtifact:
        artifact = SynthesisArtifact(
            artifact_id=generate_entry_id(),
            source_type=source_type,
            source_id=source_id,
            version=version,
            thesis=thesis,
            created_at=_now_iso(),
            agreements=agreements or [],
            disagreements=disagreements or [],
            open_questions=open_questions or [],
            action_items=action_items or [],
        )
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            store._state.artifacts[artifact.artifact_id] = artifact
            store._save_locked()
        return artifact

    async def get(self, project: str, artifact_id: str) -> SynthesisArtifact | None:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            return store._state.artifacts.get(artifact_id)

    async def list(
        self,
        project: str,
        *,
        source_type: SourceType | None = None,
    ) -> list_type[SynthesisArtifact]:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            artifacts = list(store._state.artifacts.values())
        if source_type is not None:
            artifacts = [a for a in artifacts if a.source_type == source_type]
        artifacts.sort(key=lambda a: a.created_at, reverse=True)
        return artifacts

    async def update_version(
        self,
        project: str,
        artifact_id: str,
        *,
        thesis: str | None = None,
        agreements: list_type[str] | None = None,
        disagreements: list_type[str] | None = None,
        open_questions: list_type[str] | None = None,
        action_items: list_type[ActionItem] | None = None,
    ) -> SynthesisArtifact | None:
        """Create a new version in-place (bumps version, updates fields)."""
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            artifact = store._state.artifacts.get(artifact_id)
            if artifact is None:
                return None
            artifact.version += 1
            artifact.created_at = _now_iso()
            if thesis is not None:
                artifact.thesis = thesis
            if agreements is not None:
                artifact.agreements = agreements
            if disagreements is not None:
                artifact.disagreements = disagreements
            if open_questions is not None:
                artifact.open_questions = open_questions
            if action_items is not None:
                artifact.action_items = action_items
            store._save_locked()
            return artifact

    @staticmethod
    def from_discussion_record(
        record: DiscussionRecord,
    ) -> SynthesisArtifact:
        """Build an artifact from a :class:`DiscussionRecord`.

        Maps ``summary`` → ``thesis`` and copies ``action_items``.
        Does NOT persist — caller should use :meth:`create` or save
        through the facade.
        """
        return SynthesisArtifact(
            artifact_id=generate_entry_id(),
            source_type="discussion",
            source_id=record.discussion_id,
            version=1,
            thesis=record.summary or "",
            created_at=_now_iso(),
            action_items=list(record.action_items),
        )
