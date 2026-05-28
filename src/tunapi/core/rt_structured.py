"""Structured roundtable session — Participant + Utterance + Stage.

Coexists with the existing :class:`RoundtableSession` which remains
the runtime execution model.  This module provides a richer storage
model for completed sessions, enabling stage-based replay, per-role
analysis, and tunaDish UI rendering.

Each project gets its own JSON file at
``~/.tunapi/project_memory/{alias}_rt_structured.json``.
"""

from __future__ import annotations

import time
from builtins import list as list_type
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import msgspec

from ..logging import get_logger
from ..state_store import JsonStateStore
from .project_memory import generate_entry_id
from .rt_participant import RoundtableParticipant, build_participants_from_engines
from .rt_utterance import Utterance, transcript_to_utterances

if TYPE_CHECKING:
    from .roundtable import RoundtableSession

logger = get_logger(__name__)

StructuredSessionStatus = Literal["active", "completed", "cancelled"]

_STATE_VERSION = 1


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class StructuredRoundtableSession(msgspec.Struct, forbid_unknown_fields=False):
    session_id: str
    project_alias: str
    topic: str
    stages: list[str]
    participants: list[RoundtableParticipant]
    utterances: list[Utterance]
    current_stage: str
    status: StructuredSessionStatus = "active"
    created_at: str = ""
    completed_at: str | None = None


class _State(msgspec.Struct, forbid_unknown_fields=False):
    version: int = _STATE_VERSION
    sessions: dict[str, StructuredRoundtableSession] = msgspec.field(
        default_factory=dict
    )


class StructuredRoundtableStore:
    """Per-project structured roundtable session store."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._stores: dict[str, JsonStateStore[_State]] = {}

    def _store_for(self, project: str) -> JsonStateStore[_State]:
        store = self._stores.get(project)
        if store is None:
            path = self._base_dir / f"{project}_rt_structured.json"
            store = cast(
                JsonStateStore[_State],
                JsonStateStore(
                    path,
                    version=_STATE_VERSION,
                    state_type=_State,
                    state_factory=_State,
                    log_prefix="rt_structured",
                    logger=logger,
                ),
            )
            self._stores[project] = store
        return store

    async def create(
        self,
        project: str,
        *,
        session_id: str | None = None,
        topic: str,
        stages: list_type[str],
        participants: list_type[RoundtableParticipant],
        utterances: list_type[Utterance] | None = None,
    ) -> StructuredRoundtableSession:
        sid = session_id or generate_entry_id()
        session = StructuredRoundtableSession(
            session_id=sid,
            project_alias=project,
            topic=topic,
            stages=stages,
            participants=participants,
            utterances=utterances or [],
            current_stage=stages[0] if stages else "",
            created_at=_now_iso(),
        )
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            store._state.sessions[sid] = session
            store._save_locked()
        return session

    async def get(
        self, project: str, session_id: str
    ) -> StructuredRoundtableSession | None:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            return store._state.sessions.get(session_id)

    async def list(
        self,
        project: str,
        *,
        status: StructuredSessionStatus | None = None,
    ) -> list_type[StructuredRoundtableSession]:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            sessions = list(store._state.sessions.values())
        if status is not None:
            sessions = [s for s in sessions if s.status == status]
        sessions.sort(key=lambda s: s.created_at, reverse=True)
        return sessions

    async def add_utterance(
        self, project: str, session_id: str, utterance: Utterance
    ) -> bool:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            session = store._state.sessions.get(session_id)
            if session is None:
                return False
            session.utterances.append(utterance)
            if utterance.stage and utterance.stage != session.current_stage:
                session.current_stage = utterance.stage
            store._save_locked()
            return True

    async def complete(
        self, project: str, session_id: str
    ) -> StructuredRoundtableSession | None:
        return await self._transition(project, session_id, "completed")

    async def cancel(
        self, project: str, session_id: str
    ) -> StructuredRoundtableSession | None:
        return await self._transition(project, session_id, "cancelled")

    async def _transition(
        self,
        project: str,
        session_id: str,
        target: StructuredSessionStatus,
    ) -> StructuredRoundtableSession | None:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            session = store._state.sessions.get(session_id)
            if session is None:
                return None
            session.status = target
            if target in ("completed", "cancelled"):
                session.completed_at = _now_iso()
            store._save_locked()
            return session

    @staticmethod
    def from_roundtable_session(
        session: RoundtableSession,
        project: str,
    ) -> StructuredRoundtableSession:
        """Convert a flat :class:`RoundtableSession` to structured form.

        Does NOT persist — caller should use :meth:`create` or
        facade convenience methods.
        """
        participants = build_participants_from_engines(session.engines)
        stages = [f"round_{i}" for i in range(1, session.total_rounds + 1)]
        utterances = transcript_to_utterances(
            session.transcript,
            participants,
            total_rounds=session.total_rounds,
        )
        return StructuredRoundtableSession(
            session_id=session.thread_id,
            project_alias=project,
            topic=session.topic,
            stages=stages,
            participants=participants,
            utterances=utterances,
            current_stage=stages[-1] if stages else "",
            status="completed" if session.completed else "active",
            created_at=_now_iso(),
            completed_at=_now_iso() if session.completed else None,
        )
