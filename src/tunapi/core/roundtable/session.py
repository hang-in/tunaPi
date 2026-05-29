"""Roundtable session model + persistence (transport-agnostic)."""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import anyio

from ...runner_bridge import ExecBridgeConfig
from ...transport_runtime import TransportRuntime
from ...utils.json_state import atomic_write_json


@runtime_checkable
class RoundtableBridgeCfg(Protocol):
    @property
    def runtime(self) -> TransportRuntime: ...

    @property
    def exec_cfg(self) -> ExecBridgeConfig: ...


@dataclass(slots=True)
class RoundtableSession:
    thread_id: str
    channel_id: str
    topic: str
    engines: list[str]
    total_rounds: int
    current_round: int = 0
    transcript: list[tuple[str, str]] = field(default_factory=list)
    cancel_event: anyio.Event = field(default_factory=anyio.Event)
    completed: bool = False

    def to_dict(self) -> dict:
        """Serialize to dict (excludes cancel_event)."""
        return {
            "thread_id": self.thread_id,
            "channel_id": self.channel_id,
            "topic": self.topic,
            "engines": self.engines,
            "total_rounds": self.total_rounds,
            "current_round": self.current_round,
            "transcript": self.transcript,
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RoundtableSession:
        """Deserialize from dict."""
        return cls(
            thread_id=data["thread_id"],
            channel_id=data["channel_id"],
            topic=data["topic"],
            engines=data["engines"],
            total_rounds=data["total_rounds"],
            current_round=data.get("current_round", 0),
            transcript=[tuple(t) for t in data.get("transcript", [])],
            completed=data.get("completed", False),
        )


class RoundtableStore:
    """Persistent store for roundtable sessions.

    Active sessions are in-memory only.  Completed sessions are persisted
    to a JSON file so that ``!rt follow`` works after restarts.
    """

    def __init__(self, persist_path: Path | None = None) -> None:
        self._sessions: dict[str, RoundtableSession] = {}
        self._persist_path = persist_path
        if persist_path:
            self._load()

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        if not self._persist_path or not self._persist_path.exists():
            return
        with contextlib.suppress(Exception):
            data = json.loads(self._persist_path.read_text())
            for entry in data.get("sessions", []):
                session = RoundtableSession.from_dict(entry)
                if session.completed:
                    self._sessions[session.thread_id] = session

    def _save(self) -> None:
        if not self._persist_path:
            return
        with contextlib.suppress(Exception):
            entries = [s.to_dict() for s in self._sessions.values() if s.completed]
            atomic_write_json(
                self._persist_path,
                {"version": 1, "sessions": entries},
            )

    # -- public API ------------------------------------------------------------

    def get(self, thread_id: str) -> RoundtableSession | None:
        return self._sessions.get(thread_id)

    def get_completed(self, thread_id: str) -> RoundtableSession | None:
        s = self._sessions.get(thread_id)
        return s if s and s.completed else None

    def put(self, session: RoundtableSession) -> None:
        self._sessions[session.thread_id] = session

    def remove(self, thread_id: str) -> RoundtableSession | None:
        result = self._sessions.pop(thread_id, None)
        self._save()
        return result

    def complete(self, thread_id: str) -> None:
        session = self._sessions.get(thread_id)
        if session:
            session.completed = True
            self._save()
