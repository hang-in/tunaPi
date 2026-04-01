"""Tests for DiscordPrefsStore."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tunapi.discord.prefs import DiscordPrefsStore


@pytest.mark.anyio
async def test_set_and_get_model_override_persists(tmp_path: Path) -> None:
    config_path = tmp_path / "tunapi.toml"
    store = DiscordPrefsStore(config_path)

    await store.set_model_override(123, 456, "claude", "claude-3")
    assert await store.get_model_override(123, 456, "claude") == "claude-3"

    reloaded = DiscordPrefsStore(config_path)
    assert await reloaded.get_model_override(123, 456, "claude") == "claude-3"

    await reloaded.set_model_override(123, 456, "claude", None)
    cleared = DiscordPrefsStore(config_path)
    assert await cleared.get_model_override(123, 456, "claude") is None

    prefs_path = config_path.parent / "discord_prefs.json"
    payload = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert payload["channels"] == {}


@pytest.mark.anyio
async def test_migrates_legacy_prefs_from_state_file(tmp_path: Path) -> None:
    config_path = tmp_path / "tunapi.toml"
    legacy_path = tmp_path / "discord_state.json"

    legacy = {
        "version": 2,
        "channels": {
            "123:456": {
                "context": None,
                "sessions": None,
                "model_overrides": {"claude": "claude-3"},
                "reasoning_overrides": {"codex": "high"},
                "trigger_mode": "mentions",
                "default_engine": "codex",
            }
        },
        "guilds": {},
    }
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")

    store = DiscordPrefsStore(config_path)
    await store.ensure_loaded()

    assert await store.get_model_override(123, 456, "claude") == "claude-3"
    assert await store.get_reasoning_override(123, 456, "codex") == "high"
    assert await store.get_trigger_mode(123, 456) == "mentions"
    assert await store.get_default_engine(123, 456) == "codex"

    prefs_path = config_path.parent / "discord_prefs.json"
    assert prefs_path.exists()
