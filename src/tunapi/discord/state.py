"""State management for Discord transport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import msgspec

from .types import DiscordChannelContext, DiscordThreadContext

STATE_VERSION = 2


class DiscordChannelStateData(msgspec.Struct):
    """State data for a single channel or thread."""

    # For channels: {"project", "worktrees_dir", "default_engine", "worktree_base"}
    # For threads: {"project", "branch", "worktrees_dir", "default_engine"}
    context: dict[str, str] | None = None
    sessions: dict[str, str] | None = None  # engine_id -> resume_token


class DiscordGuildData(msgspec.Struct):
    """State data for a guild."""

    startup_channel_id: int | None = None


class DiscordState(msgspec.Struct):
    """Root state structure."""

    version: int = STATE_VERSION
    channels: dict[str, DiscordChannelStateData] = msgspec.field(default_factory=dict)
    guilds: dict[str, DiscordGuildData] = msgspec.field(default_factory=dict)


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically using a temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    content = json.dumps(data, indent=2, ensure_ascii=False)
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


DEFAULT_STATE_PATH = Path.home() / ".tunapi" / "discord_state.json"


class DiscordStateStore:
    """State store for Discord channel mappings and sessions."""

    def __init__(self, config_path: Path | None = None) -> None:
        if config_path is not None:
            self._path = config_path.parent / "discord_state.json"
        else:
            self._path = DEFAULT_STATE_PATH
        self._lock = anyio.Lock()
        self._loaded = False
        self._mtime_ns: int | None = None
        self._state = DiscordState()

    def _stat_mtime_ns(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def _reload_if_needed(self) -> None:
        current = self._stat_mtime_ns()
        if self._loaded and current == self._mtime_ns:
            return
        self._load()

    def _load(self) -> None:
        self._loaded = True
        self._mtime_ns = self._stat_mtime_ns()
        if self._mtime_ns is None:
            self._state = DiscordState()
            return
        try:
            payload = msgspec.json.decode(self._path.read_bytes(), type=DiscordState)
        except Exception:  # noqa: BLE001
            self._state = DiscordState()
            return
        # Handle migration from version 1 to 2.
        if payload.version < STATE_VERSION:
            payload = DiscordState(
                version=STATE_VERSION,
                channels=payload.channels,
                guilds=payload.guilds,
            )
            self._state = payload
            self._save()
            return
        self._state = payload

    def _save(self) -> None:
        payload = msgspec.to_builtins(self._state)
        _atomic_write_json(self._path, payload)
        self._mtime_ns = self._stat_mtime_ns()

    @staticmethod
    def _channel_key(guild_id: int | None, channel_id: int) -> str:
        if guild_id is not None:
            return f"{guild_id}:{channel_id}"
        return str(channel_id)

    @classmethod
    def _session_key(
        cls, guild_id: int | None, channel_id: int, author_id: int | None
    ) -> str:
        base = cls._channel_key(guild_id, channel_id)
        if author_id is None:
            return base
        return f"{base}:{author_id}"

    async def get_context(
        self, guild_id: int | None, channel_id: int
    ) -> DiscordChannelContext | DiscordThreadContext | None:
        """Get the context for a channel or thread.

        Returns DiscordChannelContext for channels (no branch),
        or DiscordThreadContext for threads (with branch).
        """
        async with self._lock:
            self._reload_if_needed()
            key = self._channel_key(guild_id, channel_id)
            channel_data = self._state.channels.get(key)
            if channel_data is None or channel_data.context is None:
                return None
            ctx = channel_data.context
            project = ctx.get("project")
            if project is None:
                return None

            # Check if this is a thread context (has branch) or channel context
            branch = ctx.get("branch")
            if branch is not None:
                # Thread context
                return DiscordThreadContext(
                    project=project,
                    branch=branch,
                    worktrees_dir=ctx.get("worktrees_dir", ".worktrees"),
                    default_engine=ctx.get("default_engine", "claude"),
                )
            else:
                # Channel context
                return DiscordChannelContext(
                    project=project,
                    worktrees_dir=ctx.get("worktrees_dir", ".worktrees"),
                    default_engine=ctx.get("default_engine", "claude"),
                    worktree_base=ctx.get("worktree_base", "main"),
                )

    async def set_context(
        self,
        guild_id: int | None,
        channel_id: int,
        context: DiscordChannelContext | DiscordThreadContext | None,
    ) -> None:
        """Set the context for a channel or thread."""
        async with self._lock:
            self._reload_if_needed()
            key = self._channel_key(guild_id, channel_id)
            if key not in self._state.channels:
                self._state.channels[key] = DiscordChannelStateData()
            if context is None:
                self._state.channels[key].context = None
            elif isinstance(context, DiscordThreadContext):
                # Thread context (with branch)
                self._state.channels[key].context = {
                    "project": context.project,
                    "branch": context.branch,
                    "worktrees_dir": context.worktrees_dir,
                    "default_engine": context.default_engine,
                }
            else:
                # Channel context (no branch)
                self._state.channels[key].context = {
                    "project": context.project,
                    "worktrees_dir": context.worktrees_dir,
                    "default_engine": context.default_engine,
                    "worktree_base": context.worktree_base,
                }
            self._save()

    async def get_session(
        self,
        guild_id: int | None,
        channel_id: int,
        engine_id: str,
        *,
        author_id: int | None = None,
    ) -> str | None:
        """Get the resume token for a session."""
        async with self._lock:
            self._reload_if_needed()
            key = self._session_key(guild_id, channel_id, author_id)
            channel_data = self._state.channels.get(key)
            if channel_data is None or channel_data.sessions is None:
                return None
            return channel_data.sessions.get(engine_id)

    async def set_session(
        self,
        guild_id: int | None,
        channel_id: int,
        engine_id: str,
        resume_token: str | None,
        *,
        author_id: int | None = None,
    ) -> None:
        """Set or clear the resume token for a session."""
        async with self._lock:
            self._reload_if_needed()
            key = self._session_key(guild_id, channel_id, author_id)
            if key not in self._state.channels:
                self._state.channels[key] = DiscordChannelStateData()
            if self._state.channels[key].sessions is None:
                self._state.channels[key].sessions = {}
            if resume_token is None:
                self._state.channels[key].sessions.pop(engine_id, None)
            else:
                self._state.channels[key].sessions[engine_id] = resume_token
            self._save()

    async def clear_channel(self, guild_id: int | None, channel_id: int) -> None:
        """Clear all state for a channel."""
        async with self._lock:
            self._reload_if_needed()
            key = self._channel_key(guild_id, channel_id)
            self._state.channels.pop(key, None)
            prefix = f"{key}:"
            for entry in list(self._state.channels):
                if entry.startswith(prefix):
                    self._state.channels.pop(entry, None)
            self._save()

    async def clear_sessions(
        self,
        guild_id: int | None,
        channel_id: int,
        *,
        author_id: int | None = None,
    ) -> None:
        """Clear all session tokens for a channel/thread."""
        async with self._lock:
            self._reload_if_needed()
            if author_id is not None:
                key = self._session_key(guild_id, channel_id, author_id)
                self._state.channels.pop(key, None)
                self._save()
                return

            key = self._channel_key(guild_id, channel_id)
            channel_data = self._state.channels.get(key)
            if channel_data is not None:
                channel_data.sessions = None

            prefix = f"{key}:"
            for entry in list(self._state.channels):
                if entry.startswith(prefix):
                    self._state.channels.pop(entry, None)

            self._save()

    # Guild-level methods
    async def get_startup_channel(self, guild_id: int) -> int | None:
        """Get the startup channel for a guild."""
        async with self._lock:
            self._reload_if_needed()
            key = str(guild_id)
            guild_data = self._state.guilds.get(key)
            if guild_data is None:
                return None
            return guild_data.startup_channel_id

    async def set_startup_channel(self, guild_id: int, channel_id: int | None) -> None:
        """Set the startup channel for a guild."""
        async with self._lock:
            self._reload_if_needed()
            key = str(guild_id)
            if key not in self._state.guilds:
                self._state.guilds[key] = DiscordGuildData()
            self._state.guilds[key].startup_channel_id = channel_id
            self._save()
