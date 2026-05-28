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

import json
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
    cwd: str | None = None


class _ChannelSessions(msgspec.Struct, forbid_unknown_fields=False):
    sessions: dict[str, _SessionEntry] = msgspec.field(default_factory=dict)


class _State(msgspec.Struct, forbid_unknown_fields=False):
    version: int = STATE_VERSION
    cwd: str | None = None
    channels: dict[str, _ChannelSessions] = msgspec.field(default_factory=dict)


def _migrate_telegram_sessions(raw: bytes) -> bytes:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw
    if data.get("version") != 1:
        return raw
    if "chats" in data and "channels" not in data:
        channels = {}
        for chat_key, chat_val in data["chats"].items():
            sessions = {}
            for eng, sess in chat_val.get("sessions", {}).items():
                sessions[eng] = {
                    "value": sess.get("resume", ""),
                    "cwd": data.get("cwd"),
                }
            channels[chat_key] = {"sessions": sessions}
        data["version"] = 2
        data["channels"] = channels
        data.pop("chats", None)
        return json.dumps(data).encode("utf-8")
    return raw


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
            raw = _migrate_telegram_sessions(raw)
            payload = msgspec.json.decode(raw, type=self._state_type)
        except Exception:  # noqa: BLE001
            # Try v1 migration before giving up
            try:
                raw = self._path.read_bytes()
                raw = _migrate_telegram_sessions(raw)
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

    @staticmethod
    def _normalize_cwd(cwd: Path | None) -> str | None:
        if cwd is None:
            return None
        return str(cwd.expanduser().resolve())

    async def get(
        self, channel_id: str | int, engine: str, *, cwd: Path | None = None
    ) -> ResumeToken | None:
        """Get resume token for a specific channel+engine pair."""
        async with self._lock:
            self._reload_locked_if_needed()
            channel = self._state.channels.get(str(channel_id))
            if channel is None:
                return None
            entry = channel.sessions.get(engine)
            if entry is None:
                return None
            expected_cwd = self._normalize_cwd(cwd)
            if expected_cwd != entry.cwd:
                if expected_cwd is not None:
                    channel.sessions.pop(engine, None)
                    if not channel.sessions:
                        self._state.channels.pop(str(channel_id), None)
                    self._save_locked()
                return None
            return ResumeToken(engine=engine, value=entry.value)

    async def set(
        self, channel_id: str | int, token: ResumeToken, *, cwd: Path | None = None
    ) -> None:
        """Store a resume token (uses token.engine as key)."""
        key = str(channel_id)
        async with self._lock:
            self._reload_locked_if_needed()
            channel = self._state.channels.get(key)
            if channel is None:
                channel = _ChannelSessions()
                self._state.channels[key] = channel
            channel.sessions[token.engine] = _SessionEntry(
                value=token.value,
                cwd=self._normalize_cwd(cwd),
            )
            self._save_locked()

    async def clear(self, channel_id: str | int) -> None:
        """Clear all engine sessions for a channel (/new)."""
        key = str(channel_id)
        async with self._lock:
            self._reload_locked_if_needed()
            if self._state.channels.pop(key, None) is not None:
                self._save_locked()

    async def clear_engine(self, channel_id: str | int, engine: str) -> None:
        """Clear a specific engine session for a channel."""
        key = str(channel_id)
        async with self._lock:
            self._reload_locked_if_needed()
            channel = self._state.channels.get(key)
            if channel is None:
                return
            if channel.sessions.pop(engine, None) is not None:
                if not channel.sessions:
                    del self._state.channels[key]
                self._save_locked()

    async def has_any(self, channel_id: str | int) -> bool:
        """Check if channel has any active session (for /status)."""
        async with self._lock:
            self._reload_locked_if_needed()
            channel = self._state.channels.get(str(channel_id))
            return channel is not None and bool(channel.sessions)

    async def sync_startup_cwd(self, cwd: Path) -> bool:
        normalized = self._normalize_cwd(cwd)
        async with self._lock:
            self._reload_locked_if_needed()
            previous = self._state.cwd
            cleared = False
            if previous is not None and previous != normalized:
                self._state.channels = {}
                cleared = True
            if previous != normalized:
                self._state.cwd = normalized
                self._save_locked()
            return cleared

    # -- Telegram backward-compatibility helpers --

    async def get_session_resume(
        self, chat_id: str | int, user_id: str | int | None, engine: str
    ) -> ResumeToken | None:
        owner = "chat" if user_id is None else str(user_id)
        channel_id = f"{chat_id}:{owner}"
        from pathlib import Path

        return await self.get(channel_id, engine, cwd=Path.cwd())

    async def set_session_resume(
        self, chat_id: str | int, user_id: str | int | None, token: ResumeToken
    ) -> None:
        owner = "chat" if user_id is None else str(user_id)
        channel_id = f"{chat_id}:{owner}"
        from pathlib import Path

        await self.set(channel_id, token, cwd=Path.cwd())

    async def clear_chat_sessions(self, chat_id: str | int) -> None:
        prefix = f"{chat_id}:"
        async with self._lock:
            self._reload_locked_if_needed()
            keys_to_remove = [k for k in self._state.channels if k.startswith(prefix)]
            removed = False
            for k in keys_to_remove:
                self._state.channels.pop(k, None)
                removed = True
            if removed:
                self._save_locked()
