"""Chat preference management for Discord transport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import msgspec

PREFS_VERSION = 1


class DiscordChannelPrefsData(msgspec.Struct, forbid_unknown_fields=False):
    """Preferences data for a single channel or thread."""

    model_overrides: dict[str, str] | None = None  # engine_id -> model
    reasoning_overrides: dict[str, str] | None = None  # engine_id -> level
    trigger_mode: str | None = None  # "all" | "mentions"
    default_engine: str | None = None  # default engine override for this channel/thread


class DiscordPrefs(msgspec.Struct, forbid_unknown_fields=False):
    """Root preferences structure."""

    version: int = PREFS_VERSION
    channels: dict[str, DiscordChannelPrefsData] = msgspec.field(default_factory=dict)


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically using a temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    content = json.dumps(data, indent=2, ensure_ascii=False)
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


DEFAULT_PREFS_PATH = Path.home() / ".tunapi" / "discord_prefs.json"


class DiscordPrefsStore:
    """Preferences store for Discord channel/thread overrides and trigger modes."""

    def __init__(self, config_path: Path | None = None) -> None:
        if config_path is not None:
            self._path = config_path.parent / "discord_prefs.json"
        else:
            self._path = DEFAULT_PREFS_PATH
        self._lock = anyio.Lock()
        self._loaded = False
        self._mtime_ns: int | None = None
        self._state = DiscordPrefs()

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
            if self._try_migrate_from_state_file():
                return
            self._state = DiscordPrefs()
            return
        try:
            payload = msgspec.json.decode(self._path.read_bytes(), type=DiscordPrefs)
        except Exception:  # noqa: BLE001
            self._state = DiscordPrefs()
            return
        if payload.version < PREFS_VERSION:
            payload = DiscordPrefs(version=PREFS_VERSION, channels=payload.channels)
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

    def _try_migrate_from_state_file(self) -> bool:
        """Migrate legacy preferences stored in discord_state.json into discord_prefs.json.

        Only runs when the prefs file doesn't exist.
        """
        legacy_path = self._path.with_name("discord_state.json")
        try:
            raw = msgspec.json.decode(legacy_path.read_bytes())
        except FileNotFoundError:
            return False
        except Exception:  # noqa: BLE001
            return False

        if not isinstance(raw, dict):
            return False
        channels = raw.get("channels")
        if not isinstance(channels, dict):
            return False

        migrated: dict[str, DiscordChannelPrefsData] = {}
        for key, entry in channels.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue

            model_overrides = _coerce_str_map(entry.get("model_overrides"))
            reasoning_overrides = _coerce_str_map(entry.get("reasoning_overrides"))
            trigger_mode = _coerce_str(entry.get("trigger_mode"))
            default_engine = _coerce_str(entry.get("default_engine"))

            if (
                model_overrides
                or reasoning_overrides
                or trigger_mode is not None
                or default_engine is not None
            ):
                migrated[key] = DiscordChannelPrefsData(
                    model_overrides=model_overrides,
                    reasoning_overrides=reasoning_overrides,
                    trigger_mode=trigger_mode,
                    default_engine=default_engine,
                )

        if not migrated:
            return False

        self._state = DiscordPrefs(version=PREFS_VERSION, channels=migrated)
        self._save()
        return True

    def _maybe_prune_channel_locked(self, key: str) -> None:
        entry = self._state.channels.get(key)
        if entry is None:
            return
        if (
            entry.model_overrides
            or entry.reasoning_overrides
            or entry.trigger_mode is not None
            or entry.default_engine is not None
        ):
            return
        del self._state.channels[key]

    async def ensure_loaded(self) -> None:
        """Ensure the prefs file is loaded (and migrated if needed)."""
        async with self._lock:
            self._reload_if_needed()

    async def clear_channel(self, guild_id: int | None, channel_id: int) -> None:
        """Clear all preferences for a channel/thread."""
        async with self._lock:
            self._reload_if_needed()
            key = self._channel_key(guild_id, channel_id)
            if key not in self._state.channels:
                return
            del self._state.channels[key]
            self._save()

    async def get_model_override(
        self, guild_id: int | None, channel_id: int, engine_id: str
    ) -> str | None:
        """Get model override for an engine."""
        async with self._lock:
            self._reload_if_needed()
            key = self._channel_key(guild_id, channel_id)
            channel_data = self._state.channels.get(key)
            if channel_data is None or channel_data.model_overrides is None:
                return None
            return channel_data.model_overrides.get(engine_id)

    async def set_model_override(
        self,
        guild_id: int | None,
        channel_id: int,
        engine_id: str,
        model: str | None,
    ) -> None:
        """Set or clear model override for an engine."""
        async with self._lock:
            self._reload_if_needed()
            key = self._channel_key(guild_id, channel_id)
            if model is None:
                channel_data = self._state.channels.get(key)
                if channel_data is None or channel_data.model_overrides is None:
                    return
                channel_data.model_overrides.pop(engine_id, None)
                if not channel_data.model_overrides:
                    channel_data.model_overrides = None
                self._maybe_prune_channel_locked(key)
                self._save()
                return

            if key not in self._state.channels:
                self._state.channels[key] = DiscordChannelPrefsData()
            if self._state.channels[key].model_overrides is None:
                self._state.channels[key].model_overrides = {}
            self._state.channels[key].model_overrides[engine_id] = model
            self._save()

    async def clear_model_overrides(
        self, guild_id: int | None, channel_id: int
    ) -> None:
        """Clear all model overrides for a channel/thread."""
        async with self._lock:
            self._reload_if_needed()
            key = self._channel_key(guild_id, channel_id)
            channel_data = self._state.channels.get(key)
            if channel_data is None or channel_data.model_overrides is None:
                return
            channel_data.model_overrides = None
            self._maybe_prune_channel_locked(key)
            self._save()

    async def get_reasoning_override(
        self, guild_id: int | None, channel_id: int, engine_id: str
    ) -> str | None:
        """Get reasoning level override for an engine."""
        async with self._lock:
            self._reload_if_needed()
            key = self._channel_key(guild_id, channel_id)
            channel_data = self._state.channels.get(key)
            if channel_data is None or channel_data.reasoning_overrides is None:
                return None
            return channel_data.reasoning_overrides.get(engine_id)

    async def set_reasoning_override(
        self,
        guild_id: int | None,
        channel_id: int,
        engine_id: str,
        level: str | None,
    ) -> None:
        """Set or clear reasoning level override for an engine."""
        async with self._lock:
            self._reload_if_needed()
            key = self._channel_key(guild_id, channel_id)
            if level is None:
                channel_data = self._state.channels.get(key)
                if channel_data is None or channel_data.reasoning_overrides is None:
                    return
                channel_data.reasoning_overrides.pop(engine_id, None)
                if not channel_data.reasoning_overrides:
                    channel_data.reasoning_overrides = None
                self._maybe_prune_channel_locked(key)
                self._save()
                return

            if key not in self._state.channels:
                self._state.channels[key] = DiscordChannelPrefsData()
            if self._state.channels[key].reasoning_overrides is None:
                self._state.channels[key].reasoning_overrides = {}
            self._state.channels[key].reasoning_overrides[engine_id] = level
            self._save()

    async def clear_reasoning_overrides(
        self, guild_id: int | None, channel_id: int
    ) -> None:
        """Clear all reasoning overrides for a channel/thread."""
        async with self._lock:
            self._reload_if_needed()
            key = self._channel_key(guild_id, channel_id)
            channel_data = self._state.channels.get(key)
            if channel_data is None or channel_data.reasoning_overrides is None:
                return
            channel_data.reasoning_overrides = None
            self._maybe_prune_channel_locked(key)
            self._save()

    async def get_trigger_mode(
        self, guild_id: int | None, channel_id: int
    ) -> str | None:
        """Get trigger mode for a channel/thread."""
        async with self._lock:
            self._reload_if_needed()
            key = self._channel_key(guild_id, channel_id)
            channel_data = self._state.channels.get(key)
            if channel_data is None:
                return None
            return channel_data.trigger_mode

    async def set_trigger_mode(
        self, guild_id: int | None, channel_id: int, mode: str | None
    ) -> None:
        """Set or clear trigger mode for a channel/thread."""
        async with self._lock:
            self._reload_if_needed()
            key = self._channel_key(guild_id, channel_id)
            if mode is None:
                channel_data = self._state.channels.get(key)
                if channel_data is None or channel_data.trigger_mode is None:
                    return
                channel_data.trigger_mode = None
                self._maybe_prune_channel_locked(key)
                self._save()
                return
            if key not in self._state.channels:
                self._state.channels[key] = DiscordChannelPrefsData()
            self._state.channels[key].trigger_mode = mode
            self._save()

    async def get_default_engine(
        self, guild_id: int | None, channel_id: int
    ) -> str | None:
        """Get default engine override for a channel/thread."""
        async with self._lock:
            self._reload_if_needed()
            key = self._channel_key(guild_id, channel_id)
            channel_data = self._state.channels.get(key)
            if channel_data is None:
                return None
            return channel_data.default_engine

    async def set_default_engine(
        self, guild_id: int | None, channel_id: int, engine: str | None
    ) -> None:
        """Set or clear default engine override for a channel/thread."""
        async with self._lock:
            self._reload_if_needed()
            key = self._channel_key(guild_id, channel_id)
            if engine is None:
                channel_data = self._state.channels.get(key)
                if channel_data is None or channel_data.default_engine is None:
                    return
                channel_data.default_engine = None
                self._maybe_prune_channel_locked(key)
                self._save()
                return
            if key not in self._state.channels:
                self._state.channels[key] = DiscordChannelPrefsData()
            self._state.channels[key].default_engine = engine
            self._save()

    async def get_all_overrides(
        self, guild_id: int | None, channel_id: int
    ) -> tuple[dict[str, str] | None, dict[str, str] | None, str | None, str | None]:
        """Get all preferences for a channel/thread.

        Returns: (model_overrides, reasoning_overrides, trigger_mode, default_engine)
        """
        async with self._lock:
            self._reload_if_needed()
            key = self._channel_key(guild_id, channel_id)
            channel_data = self._state.channels.get(key)
            if channel_data is None:
                return None, None, None, None
            return (
                channel_data.model_overrides,
                channel_data.reasoning_overrides,
                channel_data.trigger_mode,
                channel_data.default_engine,
            )


def _coerce_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _coerce_str_map(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            continue
        normalized = item.strip()
        if normalized:
            out[key] = normalized
    return out or None
