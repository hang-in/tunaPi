"""Extra tests for DiscordPrefsStore — load/save, migration, model/reasoning/trigger/default_engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tunapi.discord.prefs import (
    PREFS_VERSION,
    DiscordChannelPrefsData,
    DiscordPrefs,
    DiscordPrefsStore,
    _coerce_str,
    _coerce_str_map,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_store(tmp_path: Path) -> DiscordPrefsStore:
    config_path = tmp_path / "tunapi.toml"
    return DiscordPrefsStore(config_path)


def _prefs_path(tmp_path: Path) -> Path:
    return tmp_path / "discord_prefs.json"


# ===========================================================================
# _coerce_str / _coerce_str_map (module-level helpers)
# ===========================================================================

class TestCoerceStr:
    def test_valid_string(self) -> None:
        assert _coerce_str("hello") == "hello"

    def test_whitespace_only_returns_none(self) -> None:
        assert _coerce_str("   ") is None

    def test_strips_whitespace(self) -> None:
        assert _coerce_str("  hi  ") == "hi"

    def test_non_string_returns_none(self) -> None:
        assert _coerce_str(123) is None
        assert _coerce_str(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert _coerce_str("") is None


class TestCoerceStrMap:
    def test_valid_map(self) -> None:
        assert _coerce_str_map({"a": "b"}) == {"a": "b"}

    def test_strips_values(self) -> None:
        assert _coerce_str_map({"a": "  b  "}) == {"a": "b"}

    def test_empty_values_removed(self) -> None:
        assert _coerce_str_map({"a": "  "}) is None

    def test_non_string_keys_skipped(self) -> None:
        assert _coerce_str_map({1: "v"}) is None

    def test_non_string_values_skipped(self) -> None:
        assert _coerce_str_map({"k": 42}) is None

    def test_non_dict_returns_none(self) -> None:
        assert _coerce_str_map("not a dict") is None
        assert _coerce_str_map(None) is None

    def test_mixed_valid_invalid(self) -> None:
        result = _coerce_str_map({"a": "ok", "b": 99, "c": ""})
        assert result == {"a": "ok"}


# ===========================================================================
# DiscordChannelPrefsData
# ===========================================================================

class TestDiscordChannelPrefsData:
    def test_defaults(self) -> None:
        d = DiscordChannelPrefsData()
        assert d.model_overrides is None
        assert d.reasoning_overrides is None
        assert d.trigger_mode is None
        assert d.default_engine is None


# ===========================================================================
# Load / save basics
# ===========================================================================

class TestLoadSave:
    async def test_fresh_store_returns_none(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        assert await store.get_model_override(1, 2, "claude") is None
        assert await store.get_trigger_mode(1, 2) is None
        assert await store.get_default_engine(1, 2) is None

    async def test_ensure_loaded_idempotent(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.ensure_loaded()
        await store.ensure_loaded()  # should not fail

    async def test_corrupt_file_resets(self, tmp_path: Path) -> None:
        prefs = _prefs_path(tmp_path)
        prefs.parent.mkdir(parents=True, exist_ok=True)
        prefs.write_text("NOT VALID JSON!!!", encoding="utf-8")
        store = _make_store(tmp_path)
        await store.ensure_loaded()
        # Should reset to empty state, no crash
        assert await store.get_model_override(1, 2, "claude") is None

    async def test_external_file_change_reloaded(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_model_override(1, 2, "claude", "opus")
        # Externally mutate the file
        prefs = _prefs_path(tmp_path)
        data = json.loads(prefs.read_text(encoding="utf-8"))
        data["channels"]["1:2"]["model_overrides"]["claude"] = "sonnet"
        prefs.write_text(json.dumps(data), encoding="utf-8")
        # New store should see the external change
        store2 = _make_store(tmp_path)
        assert await store2.get_model_override(1, 2, "claude") == "sonnet"


# ===========================================================================
# Model overrides
# ===========================================================================

class TestModelOverrides:
    async def test_set_get_clear(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_model_override(1, 100, "claude", "opus")
        assert await store.get_model_override(1, 100, "claude") == "opus"

        await store.set_model_override(1, 100, "claude", None)
        assert await store.get_model_override(1, 100, "claude") is None

    async def test_clear_nonexistent_is_noop(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_model_override(1, 100, "claude", None)  # no error

    async def test_clear_model_overrides_all(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_model_override(1, 100, "claude", "opus")
        await store.set_model_override(1, 100, "gemini", "pro")
        await store.clear_model_overrides(1, 100)
        assert await store.get_model_override(1, 100, "claude") is None
        assert await store.get_model_override(1, 100, "gemini") is None

    async def test_clear_model_overrides_no_channel_is_noop(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.clear_model_overrides(1, 999)  # no error

    async def test_clear_model_overrides_no_overrides_is_noop(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_trigger_mode(1, 100, "all")
        await store.clear_model_overrides(1, 100)  # model_overrides is None

    async def test_multiple_engines(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_model_override(1, 100, "claude", "opus")
        await store.set_model_override(1, 100, "gemini", "pro")
        assert await store.get_model_override(1, 100, "claude") == "opus"
        assert await store.get_model_override(1, 100, "gemini") == "pro"


# ===========================================================================
# Reasoning overrides
# ===========================================================================

class TestReasoningOverrides:
    async def test_set_get_clear(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_reasoning_override(1, 100, "claude", "high")
        assert await store.get_reasoning_override(1, 100, "claude") == "high"

        await store.set_reasoning_override(1, 100, "claude", None)
        assert await store.get_reasoning_override(1, 100, "claude") is None

    async def test_clear_nonexistent_is_noop(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_reasoning_override(1, 100, "claude", None)  # no error

    async def test_clear_reasoning_overrides_all(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_reasoning_override(1, 100, "claude", "high")
        await store.set_reasoning_override(1, 100, "codex", "medium")
        await store.clear_reasoning_overrides(1, 100)
        assert await store.get_reasoning_override(1, 100, "claude") is None

    async def test_clear_reasoning_overrides_no_channel_is_noop(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.clear_reasoning_overrides(1, 999)

    async def test_clear_reasoning_overrides_already_none(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_trigger_mode(1, 100, "all")
        await store.clear_reasoning_overrides(1, 100)


# ===========================================================================
# Trigger mode
# ===========================================================================

class TestTriggerMode:
    async def test_set_get_clear(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_trigger_mode(1, 100, "all")
        assert await store.get_trigger_mode(1, 100) == "all"

        await store.set_trigger_mode(1, 100, None)
        assert await store.get_trigger_mode(1, 100) is None

    async def test_clear_nonexistent_is_noop(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_trigger_mode(1, 100, None)

    async def test_clear_trigger_no_channel(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_trigger_mode(1, 999, None)  # no channel exists


# ===========================================================================
# Default engine
# ===========================================================================

class TestDefaultEngine:
    async def test_set_get_clear(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_default_engine(1, 100, "codex")
        assert await store.get_default_engine(1, 100) == "codex"

        await store.set_default_engine(1, 100, None)
        assert await store.get_default_engine(1, 100) is None

    async def test_clear_nonexistent_is_noop(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_default_engine(1, 100, None)

    async def test_clear_default_engine_no_channel(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_default_engine(1, 999, None)


# ===========================================================================
# get_all_overrides
# ===========================================================================

class TestGetAllOverrides:
    async def test_empty_channel(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        m, r, t, e = await store.get_all_overrides(1, 100)
        assert m is None
        assert r is None
        assert t is None
        assert e is None

    async def test_populated(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_model_override(1, 100, "claude", "opus")
        await store.set_reasoning_override(1, 100, "claude", "high")
        await store.set_trigger_mode(1, 100, "mentions")
        await store.set_default_engine(1, 100, "codex")
        m, r, t, e = await store.get_all_overrides(1, 100)
        assert m == {"claude": "opus"}
        assert r == {"claude": "high"}
        assert t == "mentions"
        assert e == "codex"


# ===========================================================================
# clear_channel
# ===========================================================================

class TestClearChannel:
    async def test_clears_everything(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_model_override(1, 100, "claude", "opus")
        await store.set_trigger_mode(1, 100, "all")
        await store.clear_channel(1, 100)
        assert await store.get_model_override(1, 100, "claude") is None
        assert await store.get_trigger_mode(1, 100) is None

    async def test_clear_nonexistent_channel_is_noop(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.clear_channel(1, 999)  # no error


# ===========================================================================
# Channel key — guild_id=None (DM case)
# ===========================================================================

class TestChannelKeyDM:
    async def test_dm_channel_uses_channel_id_only(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_model_override(None, 42, "claude", "opus")
        assert await store.get_model_override(None, 42, "claude") == "opus"
        # Verify the key format in persisted JSON
        prefs = _prefs_path(tmp_path)
        data = json.loads(prefs.read_text(encoding="utf-8"))
        assert "42" in data["channels"]

    async def test_guild_channel_uses_compound_key(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_model_override(10, 42, "claude", "opus")
        prefs = _prefs_path(tmp_path)
        data = json.loads(prefs.read_text(encoding="utf-8"))
        assert "10:42" in data["channels"]


# ===========================================================================
# Pruning — channel entry removed when all fields are None
# ===========================================================================

class TestPruning:
    async def test_channel_pruned_when_empty(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_trigger_mode(1, 100, "all")
        await store.set_trigger_mode(1, 100, None)
        prefs = _prefs_path(tmp_path)
        data = json.loads(prefs.read_text(encoding="utf-8"))
        assert data["channels"] == {}

    async def test_channel_not_pruned_when_other_fields_set(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.set_trigger_mode(1, 100, "all")
        await store.set_model_override(1, 100, "claude", "opus")
        await store.set_trigger_mode(1, 100, None)
        prefs = _prefs_path(tmp_path)
        data = json.loads(prefs.read_text(encoding="utf-8"))
        assert "1:100" in data["channels"]


# ===========================================================================
# Migration
# ===========================================================================

class TestMigration:
    async def test_no_legacy_file_no_migration(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        await store.ensure_loaded()
        assert await store.get_model_override(1, 2, "claude") is None

    async def test_legacy_invalid_json_skipped(self, tmp_path: Path) -> None:
        legacy_path = tmp_path / "discord_state.json"
        legacy_path.write_text("NOT JSON", encoding="utf-8")
        store = _make_store(tmp_path)
        await store.ensure_loaded()
        assert await store.get_model_override(1, 2, "claude") is None

    async def test_legacy_non_dict_skipped(self, tmp_path: Path) -> None:
        legacy_path = tmp_path / "discord_state.json"
        legacy_path.write_text('"just a string"', encoding="utf-8")
        store = _make_store(tmp_path)
        await store.ensure_loaded()

    async def test_legacy_no_channels_key_skipped(self, tmp_path: Path) -> None:
        legacy_path = tmp_path / "discord_state.json"
        legacy_path.write_text('{"version": 1}', encoding="utf-8")
        store = _make_store(tmp_path)
        await store.ensure_loaded()

    async def test_legacy_channels_not_dict_skipped(self, tmp_path: Path) -> None:
        legacy_path = tmp_path / "discord_state.json"
        legacy_path.write_text('{"channels": "not a dict"}', encoding="utf-8")
        store = _make_store(tmp_path)
        await store.ensure_loaded()

    async def test_legacy_empty_channels_not_migrated(self, tmp_path: Path) -> None:
        legacy_path = tmp_path / "discord_state.json"
        legacy_path.write_text('{"channels": {}}', encoding="utf-8")
        store = _make_store(tmp_path)
        await store.ensure_loaded()
        assert not _prefs_path(tmp_path).exists()

    async def test_legacy_channel_with_only_empty_values_not_migrated(self, tmp_path: Path) -> None:
        legacy_path = tmp_path / "discord_state.json"
        data = {
            "channels": {
                "1:2": {
                    "model_overrides": None,
                    "reasoning_overrides": None,
                    "trigger_mode": None,
                    "default_engine": None,
                }
            }
        }
        legacy_path.write_text(json.dumps(data), encoding="utf-8")
        store = _make_store(tmp_path)
        await store.ensure_loaded()
        assert not _prefs_path(tmp_path).exists()

    async def test_legacy_bad_entry_types_skipped(self, tmp_path: Path) -> None:
        legacy_path = tmp_path / "discord_state.json"
        data = {
            "channels": {
                123: {"model_overrides": {"claude": "opus"}},  # non-str key
                "1:2": "not a dict",  # non-dict entry
                "3:4": {"model_overrides": {"claude": "opus"}},  # valid
            }
        }
        legacy_path.write_text(json.dumps(data), encoding="utf-8")
        store = _make_store(tmp_path)
        await store.ensure_loaded()
        assert await store.get_model_override(3, 4, "claude") == "opus"

    async def test_migration_not_run_when_prefs_file_exists(self, tmp_path: Path) -> None:
        # Write a prefs file first
        prefs = _prefs_path(tmp_path)
        prefs.parent.mkdir(parents=True, exist_ok=True)
        prefs.write_text(json.dumps({"version": 1, "channels": {}}), encoding="utf-8")
        # Write a legacy file with data
        legacy_path = tmp_path / "discord_state.json"
        legacy_path.write_text(json.dumps({
            "channels": {"1:2": {"model_overrides": {"claude": "opus"}}}
        }), encoding="utf-8")
        store = _make_store(tmp_path)
        await store.ensure_loaded()
        # Legacy data should NOT be migrated since prefs file already exists
        assert await store.get_model_override(1, 2, "claude") is None


# ===========================================================================
# Version upgrade path
# ===========================================================================

class TestVersionUpgrade:
    async def test_older_version_upgraded_and_persisted(self, tmp_path: Path) -> None:
        prefs = _prefs_path(tmp_path)
        prefs.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 0,
            "channels": {"1:2": {"model_overrides": {"claude": "opus"}}},
        }
        prefs.write_text(json.dumps(data), encoding="utf-8")
        store = _make_store(tmp_path)
        await store.ensure_loaded()
        assert await store.get_model_override(1, 2, "claude") == "opus"
        # File should be rewritten with current version
        saved = json.loads(prefs.read_text(encoding="utf-8"))
        assert saved["version"] == PREFS_VERSION


# ===========================================================================
# Default config_path
# ===========================================================================

class TestDefaultPath:
    def test_none_config_path_uses_home(self) -> None:
        store = DiscordPrefsStore(config_path=None)
        assert store._path == Path.home() / ".tunapi" / "discord_prefs.json"
