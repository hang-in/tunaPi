"""Persistent chat session store (shared by Mattermost/Slack; Telegram uses its own).

Stores resume tokens per channel/engine so that conversations survive server
restarts and engine switches.  Uses ``JsonStateStore`` for versioned, atomic
JSON persistence.

Storage layout (version 2)::

    {
      "version": 2,
      "channels": {
        "<channel_id>": {
          "sessions": {
            "<engine>": {"value": "<resume_token>"}
          }
        }
      }
    }
"""

from __future__ import annotations

from pathlib import Path

import msgspec

from ..logging import get_logger
from ..model import ResumeToken
from ..state_store import JsonStateStore

logger = get_logger(__name__)

STATE_VERSION = 2


# -- v1 schema (for migration) ------------------------------------------------


class _V1Entry(msgspec.Struct, forbid_unknown_fields=False):
    engine: str
    value: str


class _V1State(msgspec.Struct, forbid_unknown_fields=False):
    version: int = 1
    sessions: dict[str, _V1Entry] = msgspec.field(default_factory=dict)


# -- v2 schema ----------------------------------------------------------------


class _SessionEntry(msgspec.Struct, forbid_unknown_fields=False):
    value: str


class _ChannelSessions(msgspec.Struct, forbid_unknown_fields=False):
    sessions: dict[str, _SessionEntry] = msgspec.field(default_factory=dict)


class _State(msgspec.Struct, forbid_unknown_fields=False):
    version: int = STATE_VERSION
    channels: dict[str, _ChannelSessions] = msgspec.field(default_factory=dict)


def _migrate_v1(raw: bytes) -> _State | None:
    """Attempt to migrate v1 data to v2 format."""
    try:
        v1 = msgspec.json.decode(raw, type=_V1State)
    except Exception:  # noqa: BLE001
        return None
    if v1.version != 1:
        return None

    channels: dict[str, _ChannelSessions] = {}
    for channel_id, entry in v1.sessions.items():
        channels[channel_id] = _ChannelSessions(
            sessions={entry.engine: _SessionEntry(value=entry.value)}
        )
    return _State(version=STATE_VERSION, channels=channels)


class ChatSessionStore(JsonStateStore[_State]):
    """Persistent per-channel, per-engine resume-token store.

    Backed by a JSON file at *path* (typically
    ``~/.tunapi/mattermost_sessions.json``).
    """

    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            version=STATE_VERSION,
            state_type=_State,
            state_factory=_State,
            log_prefix="chat_sessions",
            logger=logger,
        )

    def _load_locked(self) -> None:
        """Override to support v1 → v2 migration."""
        self._loaded = True
        self._mtime_ns = self._stat_mtime_ns()
        if self._mtime_ns is None:
            self._state = self._state_factory()
            return
        try:
            raw = self._path.read_bytes()
            payload = msgspec.json.decode(raw, type=self._state_type)
        except Exception:  # noqa: BLE001
            # Try v1 migration before giving up
            try:
                raw = self._path.read_bytes()
            except Exception:  # noqa: BLE001
                self._state = self._state_factory()
                return
            migrated = _migrate_v1(raw)
            if migrated is not None:
                logger.warning(
                    "chat_sessions.migrated_v1_to_v2",
                    path=str(self._path),
                )
                self._state = migrated
                self._save_locked()
                return
            self._state = self._state_factory()
            return
        if payload.version != self._version:
            # Version mismatch but not v1 — try v1 migration
            migrated = _migrate_v1(raw)
            if migrated is not None:
                logger.warning(
                    "chat_sessions.migrated_v1_to_v2",
                    path=str(self._path),
                )
                self._state = migrated
                self._save_locked()
                return
            logger.warning(
                "chat_sessions.version_mismatch",
                path=str(self._path),
                version=payload.version,
                expected=self._version,
            )
            self._state = self._state_factory()
            return
        self._state = payload

    async def get(self, channel_id: str, engine: str) -> ResumeToken | None:
        """Get resume token for a specific channel+engine pair."""
        async with self._lock:
            self._reload_locked_if_needed()
            channel = self._state.channels.get(channel_id)
            if channel is None:
                return None
            entry = channel.sessions.get(engine)
            if entry is None:
                return None
            return ResumeToken(engine=engine, value=entry.value)

    async def set(self, channel_id: str, token: ResumeToken) -> None:
        """Store a resume token (uses token.engine as key)."""
        async with self._lock:
            self._reload_locked_if_needed()
            channel = self._state.channels.get(channel_id)
            if channel is None:
                channel = _ChannelSessions()
                self._state.channels[channel_id] = channel
            channel.sessions[token.engine] = _SessionEntry(value=token.value)
            self._save_locked()

    async def clear(self, channel_id: str) -> None:
        """Clear all engine sessions for a channel (/new)."""
        async with self._lock:
            self._reload_locked_if_needed()
            if self._state.channels.pop(channel_id, None) is not None:
                self._save_locked()

    async def clear_engine(self, channel_id: str, engine: str) -> None:
        """Clear a specific engine session for a channel."""
        async with self._lock:
            self._reload_locked_if_needed()
            channel = self._state.channels.get(channel_id)
            if channel is None:
                return
            if channel.sessions.pop(engine, None) is not None:
                if not channel.sessions:
                    del self._state.channels[channel_id]
                self._save_locked()

    async def has_any(self, channel_id: str) -> bool:
        """Check if channel has any active session (for /status)."""
        async with self._lock:
            self._reload_locked_if_needed()
            channel = self._state.channels.get(channel_id)
            return channel is not None and bool(channel.sessions)
