from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tunapi.runtime_loader as runtime_loader
from tunapi.config import ConfigError
from tunapi.runtime_loader import resolve_default_engine, resolve_plugins_allowlist
from tunapi.settings import TunapiSettings


def test_build_runtime_spec_minimal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime_loader.shutil, "which", lambda _cmd: "/bin/echo")
    settings = TunapiSettings.model_validate(
        {
            "transport": "telegram",
            "watch_config": True,
            "transports": {"telegram": {"bot_token": "token", "chat_id": 123}},
        }
    )
    config_path = tmp_path / "tunapi.toml"
    config_path.write_text(
        'transport = "telegram"\n\n[transports.telegram]\n'
        'bot_token = "token"\nchat_id = 123\n',
        encoding="utf-8",
    )

    spec = runtime_loader.build_runtime_spec(
        settings=settings,
        config_path=config_path,
    )

    assert spec.router.default_engine == settings.default_engine
    runtime = spec.to_runtime(config_path=config_path)
    assert runtime.default_engine == settings.default_engine
    assert runtime.watch_config is True


def test_resolve_default_engine_unknown(tmp_path: Path) -> None:
    settings = TunapiSettings.model_validate(
        {
            "transport": "telegram",
            "transports": {"telegram": {"bot_token": "token", "chat_id": 123}},
        }
    )
    with pytest.raises(ConfigError, match="Unknown default engine"):
        runtime_loader.resolve_default_engine(
            override="unknown",
            settings=settings,
            config_path=tmp_path / "tunapi.toml",
            engine_ids=["codex"],
        )


class TestResolvePluginsAllowlist:
    def test_none_settings(self):
        assert resolve_plugins_allowlist(None) is None

    def test_empty_enabled(self):
        settings = MagicMock()
        settings.plugins.enabled = []
        assert resolve_plugins_allowlist(settings) is None

    def test_with_enabled(self):
        settings = MagicMock()
        settings.plugins.enabled = ["claude", "codex"]
        result = resolve_plugins_allowlist(settings)
        assert result == ["claude", "codex"]


class TestResolveDefaultEnginePush:
    def test_override(self):
        settings = MagicMock()
        settings.default_engine = "codex"
        result = resolve_default_engine(
            override="claude",
            settings=settings,
            config_path=Path("/c.toml"),
            engine_ids=["claude", "codex"],
        )
        assert result == "claude"

    def test_from_settings(self):
        settings = MagicMock()
        settings.default_engine = "codex"
        result = resolve_default_engine(
            override=None,
            settings=settings,
            config_path=Path("/c.toml"),
            engine_ids=["claude", "codex"],
        )
        assert result == "codex"

    def test_fallback_codex(self):
        settings = MagicMock()
        settings.default_engine = None
        result = resolve_default_engine(
            override=None,
            settings=settings,
            config_path=Path("/c.toml"),
            engine_ids=["claude", "codex"],
        )
        assert result == "codex"

    def test_unknown_engine(self):
        settings = MagicMock()
        settings.default_engine = "unknown"
        with pytest.raises(ConfigError, match="Unknown default engine"):
            resolve_default_engine(
                override=None,
                settings=settings,
                config_path=Path("/c.toml"),
                engine_ids=["claude", "codex"],
            )
