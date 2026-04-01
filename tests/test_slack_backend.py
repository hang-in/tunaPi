"""Tests for tunapi.slack.backend — SlackBackend setup, config, and lifecycle."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tunapi.backends import EngineBackend
from tunapi.slack.backend import SlackBackend, slack_backend, BACKEND
from tunapi.transports import SetupResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engine_backend() -> EngineBackend:
    return EngineBackend(id="codex", build_runner=lambda _cfg, _path: None)


def _fake_runtime() -> MagicMock:
    rt = MagicMock()
    rt.default_engine = "claude"
    rt.available_engine_ids.return_value = ["claude"]
    rt.projects_root = None
    rt.watch_config = False
    return rt


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestModuleExports:
    def test_backend_is_slack_backend(self):
        assert isinstance(BACKEND, SlackBackend)
        assert slack_backend is BACKEND

    def test_id_and_description(self):
        b = SlackBackend()
        assert b.id == "slack"
        assert "Slack" in b.description


# ---------------------------------------------------------------------------
# check_setup
# ---------------------------------------------------------------------------


class TestCheckSetup:
    def test_returns_ok(self):
        b = SlackBackend()
        result = b.check_setup(_engine_backend())
        assert isinstance(result, SetupResult)
        assert result.ok is True
        assert result.issues == []

    def test_with_transport_override(self):
        b = SlackBackend()
        result = b.check_setup(_engine_backend(), transport_override="slack")
        assert result.ok is True


# ---------------------------------------------------------------------------
# interactive_setup
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_interactive_setup_returns_false():
    b = SlackBackend()
    assert await b.interactive_setup(force=True) is False
    assert await b.interactive_setup(force=False) is False


# ---------------------------------------------------------------------------
# lock_token
# ---------------------------------------------------------------------------


class TestLockToken:
    def test_dict_config(self):
        b = SlackBackend()
        token = b.lock_token(
            transport_config={"bot_token": "xoxb-123"},
            _config_path=Path("x"),
        )
        assert token == "xoxb-123"

    def test_dict_config_no_token(self):
        b = SlackBackend()
        token = b.lock_token(
            transport_config={},
            _config_path=Path("x"),
        )
        assert token is None

    def test_object_config(self):
        b = SlackBackend()

        class Cfg:
            bot_token = "xoxb-obj"

        token = b.lock_token(
            transport_config=Cfg(),
            _config_path=Path("x"),
        )
        assert token == "xoxb-obj"

    def test_object_config_no_attr(self):
        b = SlackBackend()
        token = b.lock_token(
            transport_config=object(),
            _config_path=Path("x"),
        )
        assert token is None


# ---------------------------------------------------------------------------
# build_and_run — validation
# ---------------------------------------------------------------------------


class TestBuildAndRunValidation:
    def test_non_dict_raises_type_error(self):
        b = SlackBackend()
        with pytest.raises(TypeError, match="dict"):
            b.build_and_run(
                transport_config="not-a-dict",
                config_path=Path("x"),
                runtime=_fake_runtime(),
                final_notify=True,
                default_engine_override=None,
            )

    def test_missing_tokens_raises_value_error(self):
        b = SlackBackend()
        with pytest.raises(ValueError, match="bot_token and app_token"):
            b.build_and_run(
                transport_config={"bot_token": "", "app_token": ""},
                config_path=Path("x"),
                runtime=_fake_runtime(),
                final_notify=True,
                default_engine_override=None,
            )

    def test_missing_bot_token_raises(self):
        b = SlackBackend()
        with pytest.raises(ValueError, match="bot_token and app_token"):
            b.build_and_run(
                transport_config={"bot_token": "", "app_token": "xapp-1"},
                config_path=Path("x"),
                runtime=_fake_runtime(),
                final_notify=True,
                default_engine_override=None,
            )

    def test_missing_app_token_raises(self):
        b = SlackBackend()
        with pytest.raises(ValueError, match="bot_token and app_token"):
            b.build_and_run(
                transport_config={"bot_token": "xoxb-1", "app_token": ""},
                config_path=Path("x"),
                runtime=_fake_runtime(),
                final_notify=True,
                default_engine_override=None,
            )


# ---------------------------------------------------------------------------
# build_and_run — prepare_only mode
# ---------------------------------------------------------------------------


class TestBuildAndRunPrepare:
    def test_prepare_only_stores_pending_run(self, monkeypatch):
        """When _prepare_only=True, stores config without calling anyio.run."""
        b = SlackBackend()
        b._prepare_only = True

        config = {
            "bot_token": "xoxb-test",
            "app_token": "xapp-test",
            "channel_id": "C123",
            "session_mode": "stateless",
            "trigger_mode": "mentions",
        }

        # Patch SlackClient and SlackTransport to avoid real initialization
        monkeypatch.setattr(
            "tunapi.slack.backend.SlackClient",
            lambda _bt, _at: MagicMock(),
        )
        monkeypatch.setattr(
            "tunapi.slack.backend.SlackTransport",
            lambda _bot: MagicMock(),
        )
        monkeypatch.setattr(
            "tunapi.slack.backend.SlackPresenter",
            lambda **kw: MagicMock(),
        )

        b.build_and_run(
            transport_config=config,
            config_path=Path("x"),
            runtime=_fake_runtime(),
            final_notify=True,
            default_engine_override="claude",
        )

        assert b._prepared is True
        assert hasattr(b, "_pending_run")
        p = b._pending_run
        assert p["channel_id"] == "C123"
        assert p["default_engine_override"] == "claude"
        assert p["trigger_mode"] == "mentions"

    def test_default_config_values(self, monkeypatch):
        """Default config values are applied when keys missing."""
        b = SlackBackend()
        b._prepare_only = True

        config = {
            "bot_token": "xoxb-test",
            "app_token": "xapp-test",
        }

        monkeypatch.setattr(
            "tunapi.slack.backend.SlackClient",
            lambda _bt, _at: MagicMock(),
        )
        monkeypatch.setattr(
            "tunapi.slack.backend.SlackTransport",
            lambda _bot: MagicMock(),
        )
        monkeypatch.setattr(
            "tunapi.slack.backend.SlackPresenter",
            lambda **kw: MagicMock(),
        )

        b.build_and_run(
            transport_config=config,
            config_path=Path("x"),
            runtime=_fake_runtime(),
            final_notify=True,
            default_engine_override=None,
        )

        p = b._pending_run
        assert p["channel_id"] is None
        assert p["session_mode"] == "stateless"
        assert p["show_resume_line"] is True
        assert p["message_overflow"] == "trim"
        assert p["allowed_channel_ids"] == ()
        assert p["allowed_user_ids"] == ()
        assert p["trigger_mode"] == "mentions"

    def test_non_dict_files_voice_ignored(self, monkeypatch):
        """Non-dict files/voice configs are converted to empty dict."""
        b = SlackBackend()
        b._prepare_only = True

        config = {
            "bot_token": "xoxb-test",
            "app_token": "xapp-test",
            "files": "invalid",
            "voice": 123,
        }

        monkeypatch.setattr(
            "tunapi.slack.backend.SlackClient",
            lambda _bt, _at: MagicMock(),
        )
        monkeypatch.setattr(
            "tunapi.slack.backend.SlackTransport",
            lambda _bot: MagicMock(),
        )
        monkeypatch.setattr(
            "tunapi.slack.backend.SlackPresenter",
            lambda **kw: MagicMock(),
        )

        b.build_and_run(
            transport_config=config,
            config_path=Path("x"),
            runtime=_fake_runtime(),
            final_notify=True,
            default_engine_override=None,
        )

        p = b._pending_run
        assert p["files_cfg"] == {}
        assert p["voice_cfg"] == {}


# ---------------------------------------------------------------------------
# async_run — auth failure
# ---------------------------------------------------------------------------


class TestAsyncRun:
    @pytest.mark.anyio
    async def test_auth_failure_raises(self, monkeypatch):
        """Auth failure raises RuntimeError."""
        b = SlackBackend()

        auth_result = MagicMock()
        auth_result.ok = False
        auth_result.error = "invalid_auth"

        bot = AsyncMock()
        bot.auth_test.return_value = auth_result

        b._pending_run = {
            "bot": bot,
            "transport": MagicMock(),
            "runtime": _fake_runtime(),
            "files_cfg": {},
            "voice_cfg": {},
            "channel_id": None,
            "startup_msg": "",
            "exec_cfg": MagicMock(),
            "session_mode": "stateless",
            "show_resume_line": True,
            "allowed_channel_ids": (),
            "allowed_user_ids": (),
            "message_overflow": "trim",
            "trigger_mode": "mentions",
            "transport_config": {},
            "default_engine_override": None,
        }

        with pytest.raises(RuntimeError, match="Slack auth failed"):
            await b.async_run()

    @pytest.mark.anyio
    async def test_auth_no_user_id_warns(self, monkeypatch):
        """Auth success but no user_id logs warning."""
        b = SlackBackend()

        auth_result = MagicMock()
        auth_result.ok = True
        auth_result.user_id = ""
        auth_result.user = "bot"

        bot = AsyncMock()
        bot.auth_test.return_value = auth_result

        transport = AsyncMock()

        b._pending_run = {
            "bot": bot,
            "transport": transport,
            "runtime": _fake_runtime(),
            "files_cfg": {},
            "voice_cfg": {},
            "channel_id": None,
            "startup_msg": "",
            "exec_cfg": MagicMock(),
            "session_mode": "stateless",
            "show_resume_line": True,
            "allowed_channel_ids": (),
            "allowed_user_ids": (),
            "message_overflow": "trim",
            "trigger_mode": "mentions",
            "transport_config": {},
            "default_engine_override": None,
        }

        # Patch run_main_loop at the import location inside async_run
        with patch("tunapi.slack.loop.run_main_loop", new_callable=AsyncMock) as mock_loop:
            await b.async_run()
            mock_loop.assert_awaited_once()

        # Transport should be closed in finally
        transport.close.assert_awaited_once()
