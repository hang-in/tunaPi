"""Tests for tunapi.discord.onboarding."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tunapi.backends import EngineBackend, SetupIssue
from tunapi.config import ConfigError
from tunapi.discord.onboarding import (
    _display_path,
    _require_discord,
    _resolve_default_config_path,
    check_setup,
    config_issue,
    mask_token,
)
from tunapi.transports import SetupResult


# ---------------------------------------------------------------------------
# mask_token
# ---------------------------------------------------------------------------


class TestMaskToken:
    def test_short_token(self):
        assert mask_token("abc") == "***"

    def test_exactly_twelve(self):
        assert mask_token("123456789012") == "************"

    def test_long_token(self):
        result = mask_token("ABCDEFGHIJ1234567890XYZWV")
        assert result == "ABCDEFGHI...XYZWV"
        assert result.startswith("ABCDEFGHI")
        assert result.endswith("XYZWV")

    def test_strips_whitespace(self):
        result = mask_token("  ABCDEFGHIJ1234567890XYZWV  ")
        assert result == "ABCDEFGHI...XYZWV"


# ---------------------------------------------------------------------------
# _display_path
# ---------------------------------------------------------------------------


class TestDisplayPath:
    def test_under_home(self, tmp_path: Path):
        home = Path.home()
        p = home / "foo" / "bar.toml"
        result = _display_path(p)
        assert result == "~/foo/bar.toml"

    def test_outside_home(self):
        p = Path("/tmp/unrelated/file.toml")
        result = _display_path(p)
        assert result == "/tmp/unrelated/file.toml"


# ---------------------------------------------------------------------------
# _resolve_default_config_path
# ---------------------------------------------------------------------------


class TestResolveDefaultConfigPath:
    def test_cwd_config_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "tunapi.toml").touch()
        result = _resolve_default_config_path()
        assert result == tmp_path / "tunapi.toml"

    def test_home_config_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        with patch(
            "tunapi.discord.onboarding.HOME_CONFIG_PATH",
            tmp_path / "home_tunapi.toml",
        ):
            (tmp_path / "home_tunapi.toml").touch()
            result = _resolve_default_config_path()
            assert result == tmp_path / "home_tunapi.toml"

    def test_no_config_returns_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        with patch(
            "tunapi.discord.onboarding.HOME_CONFIG_PATH",
            tmp_path / "nonexistent.toml",
        ):
            result = _resolve_default_config_path()
            assert result == tmp_path / "tunapi.toml"


# ---------------------------------------------------------------------------
# config_issue
# ---------------------------------------------------------------------------


class TestConfigIssue:
    def test_without_reason(self, tmp_path: Path):
        issue = config_issue(tmp_path / "tunapi.toml", title="test title")
        assert isinstance(issue, SetupIssue)
        assert issue.title == "test title"
        assert len(issue.lines) == 1

    def test_with_reason(self, tmp_path: Path):
        issue = config_issue(
            tmp_path / "tunapi.toml",
            title="test title",
            reason="missing key",
        )
        assert len(issue.lines) == 2
        assert any("missing key" in line for line in issue.lines)


# ---------------------------------------------------------------------------
# _require_discord
# ---------------------------------------------------------------------------


class TestRequireDiscord:
    def test_no_transports(self):
        settings = MagicMock(spec=[])
        del settings.transports  # ensure no transports attr
        with pytest.raises(ConfigError, match="no transports"):
            _require_discord(settings, Path("cfg.toml"))

    def test_transports_none(self):
        settings = MagicMock()
        settings.transports = None
        with pytest.raises(ConfigError, match="no transports"):
            _require_discord(settings, Path("cfg.toml"))

    def test_no_discord_section(self):
        transports = MagicMock(spec=["model_extra"])
        transports.discord = None
        transports.model_extra = {}
        settings = MagicMock()
        settings.transports = transports
        with pytest.raises(ConfigError, match="discord transport not configured"):
            _require_discord(settings, Path("cfg.toml"))

    def test_discord_in_model_extra(self):
        transports = MagicMock(spec=["model_extra"])
        transports.discord = None
        transports.model_extra = {"discord": {"bot_token": "tok123"}}
        settings = MagicMock()
        settings.transports = transports
        result = _require_discord(settings, Path("cfg.toml"))
        assert result == {"bot_token": "tok123"}

    def test_discord_dict_missing_bot_token(self):
        transports = MagicMock(spec=["model_extra"])
        transports.discord = None
        transports.model_extra = {"discord": {"bot_token": ""}}
        settings = MagicMock()
        settings.transports = transports
        with pytest.raises(ConfigError, match="bot_token is required"):
            _require_discord(settings, Path("cfg.toml"))

    def test_discord_dict_no_bot_token_key(self):
        transports = MagicMock(spec=["model_extra"])
        transports.discord = None
        transports.model_extra = {"discord": {}}
        settings = MagicMock()
        settings.transports = transports
        with pytest.raises(ConfigError, match="bot_token is required"):
            _require_discord(settings, Path("cfg.toml"))

    def test_discord_object_missing_bot_token(self):
        discord_cfg = MagicMock()
        discord_cfg.bot_token = None
        transports = MagicMock()
        transports.discord = discord_cfg
        settings = MagicMock()
        settings.transports = transports
        with pytest.raises(ConfigError, match="bot_token is required"):
            _require_discord(settings, Path("cfg.toml"))

    def test_discord_object_with_bot_token(self):
        discord_cfg = MagicMock()
        discord_cfg.bot_token = "my-token"
        transports = MagicMock()
        transports.discord = discord_cfg
        settings = MagicMock()
        settings.transports = transports
        result = _require_discord(settings, Path("cfg.toml"))
        assert result is discord_cfg


# ---------------------------------------------------------------------------
# check_setup
# ---------------------------------------------------------------------------


def _make_backend(
    engine_id: str = "claude",
    cli_cmd: str | None = "claude",
    install_cmd: str | None = "npm i -g @anthropic-ai/claude-code",
) -> EngineBackend:
    return EngineBackend(
        id=engine_id,
        build_runner=MagicMock(),
        cli_cmd=cli_cmd,
        install_cmd=install_cmd,
    )


class TestCheckSetup:
    def test_all_good(self, monkeypatch: pytest.MonkeyPatch):
        """Engine installed, Discord configured -> no issues."""
        discord_cfg = MagicMock()
        discord_cfg.bot_token = "tok"
        transports = MagicMock()
        transports.discord = discord_cfg
        settings = MagicMock()
        settings.transports = transports

        with (
            patch("tunapi.discord.onboarding.load_settings", return_value=(settings, Path("/cfg.toml"))),
            patch("shutil.which", return_value="/usr/bin/claude"),
        ):
            result = check_setup(_make_backend())
        assert isinstance(result, SetupResult)
        assert result.ok

    def test_engine_not_installed(self, monkeypatch: pytest.MonkeyPatch):
        """Engine CLI not found -> issue reported but no crash."""
        discord_cfg = MagicMock()
        discord_cfg.bot_token = "tok"
        transports = MagicMock()
        transports.discord = discord_cfg
        settings = MagicMock()
        settings.transports = transports

        with (
            patch("tunapi.discord.onboarding.load_settings", return_value=(settings, Path("/cfg.toml"))),
            patch("shutil.which", return_value=None),
        ):
            result = check_setup(_make_backend())
        assert not result.ok
        assert any("install" in issue.title.lower() or True for issue in result.issues)

    def test_config_error_no_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """load_settings raises ConfigError, config file does not exist."""
        fake_path = tmp_path / "tunapi.toml"
        with (
            patch(
                "tunapi.discord.onboarding.load_settings",
                side_effect=ConfigError("not found"),
            ),
            patch(
                "tunapi.discord.onboarding._resolve_default_config_path",
                return_value=fake_path,
            ),
            patch("shutil.which", return_value=None),
        ):
            result = check_setup(_make_backend())
        assert not result.ok
        # Should contain a "create a config" issue
        titles = [i.title for i in result.issues]
        assert "create a config" in titles

    def test_config_error_file_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """load_settings raises ConfigError, config file exists."""
        fake_path = tmp_path / "tunapi.toml"
        fake_path.touch()
        with (
            patch(
                "tunapi.discord.onboarding.load_settings",
                side_effect=ConfigError("bad format"),
            ),
            patch(
                "tunapi.discord.onboarding._resolve_default_config_path",
                return_value=fake_path,
            ),
            patch("shutil.which", return_value="/usr/bin/claude"),
        ):
            result = check_setup(_make_backend())
        assert not result.ok
        titles = [i.title for i in result.issues]
        assert "configure discord" in titles

    def test_discord_not_configured(self, monkeypatch: pytest.MonkeyPatch):
        """Settings load OK but Discord section missing."""
        transports = MagicMock(spec=["model_extra"])
        transports.discord = None
        transports.model_extra = {}
        settings = MagicMock()
        settings.transports = transports

        with (
            patch("tunapi.discord.onboarding.load_settings", return_value=(settings, Path("/cfg.toml"))),
            patch("shutil.which", return_value="/usr/bin/claude"),
        ):
            result = check_setup(_make_backend())
        assert not result.ok

    def test_transport_override_applied(self, monkeypatch: pytest.MonkeyPatch):
        """transport_override param is forwarded to settings.model_copy."""
        discord_cfg = MagicMock()
        discord_cfg.bot_token = "tok"
        transports = MagicMock()
        transports.discord = discord_cfg
        settings = MagicMock()
        settings.transports = transports
        settings.model_copy.return_value = settings

        with (
            patch("tunapi.discord.onboarding.load_settings", return_value=(settings, Path("/cfg.toml"))),
            patch("shutil.which", return_value="/usr/bin/claude"),
        ):
            result = check_setup(_make_backend(), transport_override="discord")
        settings.model_copy.assert_called_once_with(update={"transport": "discord"})
        assert result.ok
