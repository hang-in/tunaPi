from __future__ import annotations

from pathlib import Path
from typing import Any
import pytest

import msgspec

from tunapi.config import ConfigError
from tunapi.config_migrations import (
    _ensure_subtable,
    _migrate_legacy_telegram,
    _migrate_topics_scope,
    migrate_config,
    migrate_config_file,
)
from tunapi.core.chat_sessions import (
    STATE_VERSION,
    _V1Entry,
    _V1State,
    _migrate_v1,
)


class TestMigrateV1:
    def test_valid_v1(self):
        v1 = _V1State(
            version=1,
            sessions={"ch1": _V1Entry(engine="claude", value="tok123")},
        )
        raw = msgspec.json.encode(v1)
        result = _migrate_v1(raw)
        assert result is not None
        assert result.version == STATE_VERSION
        assert "ch1" in result.channels
        ch = result.channels["ch1"]
        assert "claude" in ch.sessions
        assert ch.sessions["claude"].value == "tok123"

    def test_wrong_version(self):
        v1 = _V1State(version=99, sessions={})
        raw = msgspec.json.encode(v1)
        result = _migrate_v1(raw)
        assert result is None

    def test_invalid_json(self):
        result = _migrate_v1(b"not json at all")
        assert result is None

    def test_multiple_channels(self):
        v1 = _V1State(
            version=1,
            sessions={
                "ch1": _V1Entry(engine="claude", value="tok1"),
                "ch2": _V1Entry(engine="codex", value="tok2"),
            },
        )
        raw = msgspec.json.encode(v1)
        result = _migrate_v1(raw)
        assert result is not None
        assert len(result.channels) == 2


class TestEnsureSubtablePush:
    def test_missing(self):
        result = _ensure_subtable({}, "key", config_path=Path("/c.toml"), label="x")
        assert result is None

    def test_valid(self):
        result = _ensure_subtable(
            {"key": {"a": 1}}, "key", config_path=Path("/c.toml"), label="x"
        )
        assert result == {"a": 1}

    def test_invalid(self):
        with pytest.raises(ConfigError):
            _ensure_subtable(
                {"key": "not a dict"},
                "key",
                config_path=Path("/c.toml"),
                label="x",
            )


class TestMigrateLegacyTelegramPush:
    def test_no_legacy(self):
        assert _migrate_legacy_telegram({}, config_path=Path("/c")) is False

    def test_with_bot_token(self):
        config: dict[str, Any] = {"bot_token": "tok", "chat_id": 123}
        result = _migrate_legacy_telegram(config, config_path=Path("/c.toml"))
        assert result is True
        assert "bot_token" not in config
        assert config["transports"]["telegram"]["bot_token"] == "tok"
        assert config["transport"] == "telegram"


class TestMigrateTopicsScopePush:
    def test_no_transports(self):
        assert _migrate_topics_scope({}, config_path=Path("/c")) is False

    def test_no_telegram(self):
        config = {"transports": {}}
        assert _migrate_topics_scope(config, config_path=Path("/c")) is False

    def test_no_topics(self):
        config = {"transports": {"telegram": {}}}
        assert _migrate_topics_scope(config, config_path=Path("/c")) is False

    def test_no_mode(self):
        config = {"transports": {"telegram": {"topics": {}}}}
        assert _migrate_topics_scope(config, config_path=Path("/c")) is False

    def test_multi_project_chat(self):
        config = {
            "transports": {"telegram": {"topics": {"mode": "multi_project_chat"}}}
        }
        result = _migrate_topics_scope(config, config_path=Path("/c"))
        assert result is True
        assert config["transports"]["telegram"]["topics"]["scope"] == "main"
        assert "mode" not in config["transports"]["telegram"]["topics"]

    def test_per_project_chat(self):
        config = {"transports": {"telegram": {"topics": {"mode": "per_project_chat"}}}}
        result = _migrate_topics_scope(config, config_path=Path("/c"))
        assert result is True
        assert config["transports"]["telegram"]["topics"]["scope"] == "projects"

    def test_invalid_mode(self):
        config = {"transports": {"telegram": {"topics": {"mode": "bad"}}}}
        with pytest.raises(ConfigError):
            _migrate_topics_scope(config, config_path=Path("/c"))

    def test_non_string_mode(self):
        config = {"transports": {"telegram": {"topics": {"mode": 123}}}}
        with pytest.raises(ConfigError):
            _migrate_topics_scope(config, config_path=Path("/c"))


class TestMigrateConfigPush:
    def test_no_migrations(self):
        result = migrate_config({}, config_path=Path("/c"))
        assert result == []

    def test_both_migrations(self):
        config: dict[str, Any] = {
            "bot_token": "tok",
            "chat_id": 1,
        }
        result = migrate_config(config, config_path=Path("/c"))
        assert "legacy-telegram" in result


class TestMigrateConfigFilePush:
    def test_no_migrations(self, tmp_path: Path):
        cfg = tmp_path / "tunapi.toml"
        cfg.write_text('transport = "telegram"\n')
        result = migrate_config_file(cfg)
        assert result == []

    def test_with_migration(self, tmp_path: Path):
        cfg = tmp_path / "tunapi.toml"
        cfg.write_text('bot_token = "tok"\nchat_id = 123\n')
        result = migrate_config_file(cfg)
        assert "legacy-telegram" in result
