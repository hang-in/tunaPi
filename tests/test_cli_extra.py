"""Extra tests for tunapi.cli.run and tunapi.cli.doctor — covers uncovered branches."""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from tunapi import cli
from tunapi.backends import EngineBackend, SetupIssue
from tunapi.config import ConfigError
from tunapi.lockfile import LockError
from tunapi.settings import TunapiSettings
from tunapi.transports import SetupResult

doctor_mod = importlib.import_module("tunapi.cli.doctor")
run_mod = importlib.import_module("tunapi.cli.run")

from tunapi.cli.doctor import (
    DoctorCheck,
    _doctor_file_checks,
    _doctor_slack_checks,
    _doctor_telegram_checks,
    _doctor_voice_checks,
    run_doctor,
)
from tunapi.cli.run import (
    _default_engine_for_setup,
    _load_settings_optional,
    _resolve_setup_engine,
    _resolve_transport_id,
    _run_auto_router,
    _setup_needs_config,
    _version_callback,
    acquire_config_lock,
    make_engine_cmd,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _DummyLock:
    released: bool = False

    def release(self) -> None:
        self.released = True


class _FakeTransport:
    id = "fake"
    description = "fake transport"
    _prepare_only = False

    def __init__(self, setup: SetupResult) -> None:
        self._setup = setup
        self.build_calls: list[dict] = []
        self.lock_calls: list[tuple] = []

    def check_setup(self, engine_backend, *, transport_override=None) -> SetupResult:
        return self._setup

    async def interactive_setup(self, *, force: bool) -> bool:
        return True

    def lock_token(self, *, transport_config: object, _config_path: Path) -> str | None:
        self.lock_calls.append((transport_config, _config_path))
        return "lock"

    def build_and_run(self, **kwargs) -> None:
        self.build_calls.append(kwargs)


def _engine_backend() -> EngineBackend:
    return EngineBackend(id="codex", build_runner=lambda _cfg, _path: None)


def _settings(**overrides) -> TunapiSettings:
    payload = {
        "transport": "telegram",
        "transports": {"telegram": {"bot_token": "token", "chat_id": 123}},
    }
    payload.update(overrides)
    return TunapiSettings.model_validate(payload)


# ===========================================================================
# run.py tests
# ===========================================================================


# ── _default_engine_for_setup ─────────────────────────────────────────


class TestDefaultEngineForSetup:
    def test_override_takes_precedence(self):
        assert _default_engine_for_setup("gemini", settings=None, config_path=None) == "gemini"

    def test_no_settings_returns_codex(self):
        assert _default_engine_for_setup(None, settings=None, config_path=None) == "codex"

    def test_settings_default_engine(self):
        s = _settings()
        result = _default_engine_for_setup(None, settings=s, config_path=Path("x"))
        assert result == s.default_engine


# ── _setup_needs_config ───────────────────────────────────────────────


class TestSetupNeedsConfig:
    def test_needs_config_true(self):
        setup = SetupResult(
            issues=[SetupIssue(title="create a config", lines=())],
            config_path=Path("x"),
        )
        assert _setup_needs_config(setup) is True

    def test_needs_config_false(self):
        setup = SetupResult(
            issues=[SetupIssue(title="engine not found", lines=())],
            config_path=Path("x"),
        )
        assert _setup_needs_config(setup) is False

    def test_configure_telegram(self):
        setup = SetupResult(
            issues=[SetupIssue(title="configure telegram", lines=())],
            config_path=Path("x"),
        )
        assert _setup_needs_config(setup) is True

    def test_configure_mattermost(self):
        setup = SetupResult(
            issues=[SetupIssue(title="configure mattermost", lines=())],
            config_path=Path("x"),
        )
        assert _setup_needs_config(setup) is True


# ── _version_callback ────────────────────────────────────────────────


class TestVersionCallback:
    def test_version_true_raises_exit(self):
        with pytest.raises(typer.Exit):
            _version_callback(True)

    def test_version_false_does_nothing(self):
        _version_callback(False)


# ── acquire_config_lock — empty error message ─────────────────────────


def test_acquire_config_lock_empty_error(monkeypatch, tmp_path):
    """When LockError has no message lines, show 'unknown error'."""
    config_path = tmp_path / "tunapi.toml"

    # Create a LockError with empty state to produce empty str
    error = LockError(path=config_path, state="")

    # Override str to return empty
    monkeypatch.setattr(LockError, "__str__", lambda self: "")

    def _raise(*_args, **_kwargs):
        raise error

    messages: list[tuple[str, bool]] = []
    monkeypatch.setattr(cli, "acquire_lock", _raise)
    monkeypatch.setattr(
        cli.typer, "echo", lambda msg, err=False: messages.append((msg, err))
    )

    with pytest.raises(typer.Exit) as exc:
        cli.acquire_config_lock(config_path, "token")

    assert exc.value.exit_code == 1
    assert any("unknown error" in msg for msg, _ in messages)


# ── _resolve_transport_id — from config ──────────────────────────────


class TestResolveTransportId:
    def test_from_config_value(self, monkeypatch):
        monkeypatch.setattr(
            cli, "load_or_init_config", lambda: ({"transport": "mattermost"}, Path("x"))
        )
        assert cli._resolve_transport_id(None) == "mattermost"

    def test_from_config_empty_string(self, monkeypatch):
        monkeypatch.setattr(
            cli, "load_or_init_config", lambda: ({"transport": ""}, Path("x"))
        )
        assert cli._resolve_transport_id(None) == "telegram"

    def test_from_config_no_transport_key(self, monkeypatch):
        monkeypatch.setattr(cli, "load_or_init_config", lambda: ({}, Path("x")))
        assert cli._resolve_transport_id(None) == "telegram"

    def test_from_config_non_string(self, monkeypatch):
        monkeypatch.setattr(
            cli, "load_or_init_config", lambda: ({"transport": 123}, Path("x"))
        )
        assert cli._resolve_transport_id(None) == "telegram"


# ── make_engine_cmd ──────────────────────────────────────────────────


def test_make_engine_cmd_name():
    cmd = make_engine_cmd("claude")
    assert cmd.__name__ == "run_claude"


# ── _run_auto_router — ConfigError in resolve ────────────────────────


def test_run_auto_router_config_error_in_resolve(monkeypatch):
    """ConfigError during engine/transport resolution shows error."""

    def _raise(_override):
        raise ConfigError("bad engine config")

    monkeypatch.setattr(cli, "_resolve_setup_engine", _raise)
    monkeypatch.setattr(cli, "setup_logging", lambda **_kwargs: None)

    with pytest.raises(typer.Exit) as exc:
        cli._run_auto_router(
            default_engine_override=None,
            transport_override=None,
            final_notify=True,
            debug=False,
            onboard=False,
        )
    assert exc.value.exit_code == 1


# ── _run_auto_router — debug mode sets env ───────────────────────────


def test_run_auto_router_debug_sets_env(monkeypatch, tmp_path):
    """Debug mode sets TUNAPI_LOG_FILE environment variable."""
    setup = SetupResult(issues=[], config_path=tmp_path / "tunapi.toml")
    transport = _FakeTransport(setup)
    config_path = tmp_path / "tunapi.toml"

    monkeypatch.setattr(
        cli,
        "_resolve_setup_engine",
        lambda _override: (None, None, None, "codex", _engine_backend()),
    )
    monkeypatch.setattr(cli, "_resolve_transport_id", lambda _override: "fake")
    monkeypatch.setattr(cli, "get_transport", lambda _id, allowlist=None: transport)
    monkeypatch.setattr(cli, "load_settings", lambda: (_settings(), config_path))

    setup_logging_calls = []
    monkeypatch.setattr(
        cli, "setup_logging", lambda **kw: setup_logging_calls.append(kw)
    )
    monkeypatch.setattr(cli, "build_runtime_spec", lambda **kw: MagicMock())
    monkeypatch.setattr(
        cli, "acquire_config_lock", lambda _p, _t, _tr=None: _DummyLock()
    )

    # Remove TUNAPI_LOG_FILE if set
    monkeypatch.delenv("TUNAPI_LOG_FILE", raising=False)

    cli._run_auto_router(
        default_engine_override=None,
        transport_override="fake",
        final_notify=True,
        debug=True,
        onboard=False,
    )

    assert os.environ.get("TUNAPI_LOG_FILE") == "debug.log"
    assert setup_logging_calls[0]["debug"] is True


# ── _run_auto_router — setup fails with non-config issue ─────────────


def test_run_auto_router_non_config_issue(monkeypatch, tmp_path):
    """Setup failure with non-config issue shows error title."""
    setup = SetupResult(
        issues=[SetupIssue(title="engine binary missing", lines=())],
        config_path=tmp_path / "tunapi.toml",
    )
    transport = _FakeTransport(setup)

    monkeypatch.setattr(
        cli,
        "_resolve_setup_engine",
        lambda _override: (None, None, None, "codex", _engine_backend()),
    )
    monkeypatch.setattr(cli, "_resolve_transport_id", lambda _override: "fake")
    monkeypatch.setattr(cli, "get_transport", lambda _id, allowlist=None: transport)
    monkeypatch.setattr(cli, "_should_run_interactive", lambda: False)
    monkeypatch.setattr(cli, "setup_logging", lambda **_kwargs: None)

    with pytest.raises(typer.Exit) as exc:
        cli._run_auto_router(
            default_engine_override=None,
            transport_override=None,
            final_notify=True,
            debug=False,
            onboard=False,
        )
    assert exc.value.exit_code == 1


# ── _run_auto_router — transport override in settings ─────────────────


def test_run_auto_router_transport_override(monkeypatch, tmp_path):
    """Transport override updates settings.transport."""
    setup = SetupResult(issues=[], config_path=tmp_path / "tunapi.toml")
    transport = _FakeTransport(setup)
    config_path = tmp_path / "tunapi.toml"
    settings = _settings()

    monkeypatch.setattr(
        cli,
        "_resolve_setup_engine",
        lambda _override: (None, None, None, "codex", _engine_backend()),
    )
    monkeypatch.setattr(cli, "_resolve_transport_id", lambda _override: "fake")
    monkeypatch.setattr(cli, "get_transport", lambda _id, allowlist=None: transport)
    monkeypatch.setattr(cli, "load_settings", lambda: (settings, config_path))
    monkeypatch.setattr(cli, "setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr(cli, "build_runtime_spec", lambda **kw: MagicMock())
    monkeypatch.setattr(
        cli, "acquire_config_lock", lambda _p, _t, _tr=None: _DummyLock()
    )

    cli._run_auto_router(
        default_engine_override=None,
        transport_override="fake",
        final_notify=True,
        debug=False,
        onboard=False,
    )

    assert transport.build_calls


# ── _run_auto_router — KeyboardInterrupt ──────────────────────────────


def test_run_auto_router_keyboard_interrupt(monkeypatch, tmp_path):
    """KeyboardInterrupt exits with code 130."""
    setup = SetupResult(issues=[], config_path=tmp_path / "tunapi.toml")
    transport = _FakeTransport(setup)
    config_path = tmp_path / "tunapi.toml"

    monkeypatch.setattr(
        cli,
        "_resolve_setup_engine",
        lambda _override: (None, None, None, "codex", _engine_backend()),
    )
    monkeypatch.setattr(cli, "_resolve_transport_id", lambda _override: "fake")
    monkeypatch.setattr(cli, "get_transport", lambda _id, allowlist=None: transport)
    monkeypatch.setattr(cli, "setup_logging", lambda **_kwargs: None)

    def _raise_keyboard():
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "load_settings", _raise_keyboard)

    with pytest.raises(typer.Exit) as exc:
        cli._run_auto_router(
            default_engine_override=None,
            transport_override=None,
            final_notify=True,
            debug=False,
            onboard=False,
        )
    assert exc.value.exit_code == 130


# ===========================================================================
# doctor.py tests
# ===========================================================================


# ── _doctor_telegram_checks — uncovered paths ────────────────────────


class _FakeTgBot:
    def __init__(self, me=None, chat=None, validate_ok=True):
        self._me = me
        self._chat = chat
        self._validate_ok = validate_ok
        self.closed = False

    async def get_me(self):
        return self._me

    async def get_chat(self, chat_id):
        return self._chat

    async def close(self):
        self.closed = True


class _FakeBotInfo:
    def __init__(self, id, username=None):
        self.id = id
        self.username = username


class _FakeChat:
    def __init__(self, type="private"):
        self.type = type


@pytest.mark.anyio
async def test_tg_checks_token_invalid(monkeypatch) -> None:
    """Invalid token produces error checks for token, chat_id, topics."""
    client = _FakeTgBot(me=None)
    # _doctor_telegram_checks uses _resolve_cli_attr("TelegramClient") which
    # reads from the tunapi.cli module
    import tunapi.cli as cli_mod

    monkeypatch.setattr(cli_mod, "TelegramClient", lambda _token: client)

    from tunapi.settings import TelegramTopicsSettings

    topics = TelegramTopicsSettings()
    checks = await _doctor_telegram_checks("bad_token", 123, topics, ())
    statuses = {c.label: c.status for c in checks}
    assert statuses["telegram token"] == "error"
    assert statuses["chat_id"] == "error"
    assert statuses["topics"] == "ok"  # disabled by default
    assert client.closed


@pytest.mark.anyio
async def test_tg_checks_token_invalid_topics_enabled(monkeypatch) -> None:
    """Invalid token with topics enabled shows topics as error."""
    client = _FakeTgBot(me=None)
    import tunapi.cli as cli_mod

    monkeypatch.setattr(cli_mod, "TelegramClient", lambda _token: client)

    from tunapi.settings import TelegramTopicsSettings

    topics = TelegramTopicsSettings.model_validate({"enabled": True})
    checks = await _doctor_telegram_checks("bad_token", 123, topics, ())
    statuses = {c.label: c.status for c in checks}
    assert statuses["topics"] == "error"
    assert "skipped" in next(c.detail for c in checks if c.label == "topics")


@pytest.mark.anyio
async def test_tg_checks_ok(monkeypatch) -> None:
    """Valid token and chat produce ok checks."""
    me = _FakeBotInfo(id=100, username="testbot")
    chat = _FakeChat(type="group")
    client = _FakeTgBot(me=me, chat=chat)
    import tunapi.cli as cli_mod

    monkeypatch.setattr(cli_mod, "TelegramClient", lambda _token: client)

    from tunapi.settings import TelegramTopicsSettings

    topics = TelegramTopicsSettings()
    checks = await _doctor_telegram_checks("good_token", 123, topics, ())
    statuses = {c.label: c.status for c in checks}
    assert statuses["telegram token"] == "ok"
    assert "@testbot" in next(c.detail for c in checks if c.label == "telegram token")
    assert statuses["chat_id"] == "ok"
    assert "group" in next(c.detail for c in checks if c.label == "chat_id")


@pytest.mark.anyio
async def test_tg_checks_chat_unreachable(monkeypatch) -> None:
    """Valid token but unreachable chat."""
    me = _FakeBotInfo(id=100, username="testbot")
    client = _FakeTgBot(me=me, chat=None)
    import tunapi.cli as cli_mod

    monkeypatch.setattr(cli_mod, "TelegramClient", lambda _token: client)

    from tunapi.settings import TelegramTopicsSettings

    topics = TelegramTopicsSettings()
    checks = await _doctor_telegram_checks("good_token", 999, topics, ())
    statuses = {c.label: c.status for c in checks}
    assert statuses["chat_id"] == "error"
    assert "unreachable" in next(c.detail for c in checks if c.label == "chat_id")


@pytest.mark.anyio
async def test_tg_checks_bot_no_username(monkeypatch) -> None:
    """Bot without username shows id=N."""
    me = _FakeBotInfo(id=100, username=None)
    chat = _FakeChat()
    client = _FakeTgBot(me=me, chat=chat)
    import tunapi.cli as cli_mod

    monkeypatch.setattr(cli_mod, "TelegramClient", lambda _token: client)

    from tunapi.settings import TelegramTopicsSettings

    topics = TelegramTopicsSettings()
    checks = await _doctor_telegram_checks("good_token", 123, topics, ())
    token_check = next(c for c in checks if c.label == "telegram token")
    assert "id=100" in token_check.detail


@pytest.mark.anyio
async def test_tg_checks_topics_enabled_ok(monkeypatch) -> None:
    """Topics enabled and validation passes."""
    me = _FakeBotInfo(id=100, username="bot")
    chat = _FakeChat()
    client = _FakeTgBot(me=me, chat=chat)
    import tunapi.cli as cli_mod

    monkeypatch.setattr(cli_mod, "TelegramClient", lambda _token: client)

    async def _validate_ok(**kwargs):
        pass

    monkeypatch.setattr(cli_mod, "_validate_topics_setup_for", _validate_ok)

    from tunapi.settings import TelegramTopicsSettings

    topics = TelegramTopicsSettings.model_validate({"enabled": True, "scope": "all"})
    checks = await _doctor_telegram_checks("good_token", 123, topics, ())
    topics_check = next(c for c in checks if c.label == "topics")
    assert topics_check.status == "ok"


@pytest.mark.anyio
async def test_tg_checks_topics_enabled_error(monkeypatch) -> None:
    """Topics enabled but validation fails."""
    me = _FakeBotInfo(id=100, username="bot")
    chat = _FakeChat()
    client = _FakeTgBot(me=me, chat=chat)
    import tunapi.cli as cli_mod

    monkeypatch.setattr(cli_mod, "TelegramClient", lambda _token: client)

    async def _validate_fail(**kwargs):
        raise ConfigError("topics misconfigured")

    monkeypatch.setattr(cli_mod, "_validate_topics_setup_for", _validate_fail)

    from tunapi.settings import TelegramTopicsSettings

    topics = TelegramTopicsSettings.model_validate({"enabled": True})
    checks = await _doctor_telegram_checks("good_token", 123, topics, ())
    topics_check = next(c for c in checks if c.label == "topics")
    assert topics_check.status == "error"
    assert "misconfigured" in topics_check.detail


@pytest.mark.anyio
async def test_tg_checks_exception(monkeypatch) -> None:
    """General exception in telegram checks."""

    class _BrokenBot:
        async def get_me(self):
            raise RuntimeError("network error")

        async def close(self):
            pass

    import tunapi.cli as cli_mod

    monkeypatch.setattr(cli_mod, "TelegramClient", lambda _token: _BrokenBot())

    from tunapi.settings import TelegramTopicsSettings

    topics = TelegramTopicsSettings()
    checks = await _doctor_telegram_checks("tok", 123, topics, ())
    assert checks[0].status == "error"
    assert "network error" in checks[0].detail


# ── _doctor_slack_checks — uncovered paths ────────────────────────────


class _FakeSlackAuth:
    def __init__(self, ok=True, user="bot", user_id="U1", error=None):
        self.ok = ok
        self.user = user
        self.user_id = user_id
        self.error = error


class _FakeSlackChannel:
    def __init__(self, name="general"):
        self.name = name


class _FakeSlackClient:
    def __init__(self, auth=None, channel=None):
        self._auth = auth or _FakeSlackAuth()
        self._channel = channel
        self.closed = False

    async def auth_test(self):
        return self._auth

    async def get_channel(self, channel_id):
        return self._channel

    async def close(self):
        self.closed = True


@pytest.mark.anyio
async def test_slack_checks_no_bot_token() -> None:
    checks = await _doctor_slack_checks("", "xapp-test", "C123")
    assert checks[0].status == "error"
    assert "bot_token" in checks[0].detail


@pytest.mark.anyio
async def test_slack_checks_no_app_token() -> None:
    checks = await _doctor_slack_checks("xoxb-test", "", "C123")
    assert checks[0].status == "error"
    assert "app_token" in checks[0].detail


@pytest.mark.anyio
async def test_slack_checks_invalid_token(monkeypatch) -> None:
    auth = _FakeSlackAuth(ok=False, error="invalid_auth")
    client = _FakeSlackClient(auth=auth)

    # Patch the import target used by _doctor_slack_checks
    monkeypatch.setattr(
        "tunapi.slack.client.SlackClient", lambda _bt, _at: client
    )

    checks = await _doctor_slack_checks("xoxb-bad", "xapp-bad", "C123")
    token_checks = [c for c in checks if c.label == "slack bot token"]
    assert token_checks
    assert token_checks[0].status == "error"
    assert "invalid_auth" in token_checks[0].detail


@pytest.mark.anyio
async def test_slack_checks_channel_unreachable(monkeypatch) -> None:
    """Valid token, but channel is unreachable."""
    auth = _FakeSlackAuth(ok=True)
    client = _FakeSlackClient(auth=auth, channel=None)

    monkeypatch.setattr(
        "tunapi.slack.client.SlackClient", lambda _bt, _at: client
    )

    checks = await _doctor_slack_checks("xoxb-ok", "xapp-ok", "C123")
    channel_checks = [c for c in checks if c.label == "channel_id"]
    assert channel_checks
    assert channel_checks[0].status == "error"
    assert "unreachable" in channel_checks[0].detail


@pytest.mark.anyio
async def test_slack_checks_no_channel_with_allowed(monkeypatch) -> None:
    """No channel_id but allowed_channel_ids set -> warning."""
    auth = _FakeSlackAuth(ok=True)
    client = _FakeSlackClient(auth=auth)

    monkeypatch.setattr(
        "tunapi.slack.client.SlackClient", lambda _bt, _at: client
    )

    checks = await _doctor_slack_checks("xoxb-ok", "xapp-ok", "", ("C1",))
    channel_checks = [c for c in checks if c.label == "channel_id"]
    assert channel_checks
    assert channel_checks[0].status == "warning"


@pytest.mark.anyio
async def test_slack_checks_no_channel_no_allowed(monkeypatch) -> None:
    """No channel_id and no allowed_channel_ids -> error."""
    auth = _FakeSlackAuth(ok=True)
    client = _FakeSlackClient(auth=auth)

    monkeypatch.setattr(
        "tunapi.slack.client.SlackClient", lambda _bt, _at: client
    )

    checks = await _doctor_slack_checks("xoxb-ok", "xapp-ok", "")
    channel_checks = [c for c in checks if c.label == "channel_id"]
    assert channel_checks
    assert channel_checks[0].status == "error"


# ── run_doctor — projects_config error ────────────────────────────────


def test_run_doctor_projects_config_error(monkeypatch) -> None:
    """ConfigError during to_projects_config shows error."""
    settings = _settings()

    monkeypatch.setattr(doctor_mod, "resolve_plugins_allowlist", lambda _s: None)
    monkeypatch.setattr(
        doctor_mod, "list_backend_ids", lambda allowlist=None: ["claude"]
    )

    def _failing_tpc(self, **kwargs):
        raise ConfigError("bad projects")

    monkeypatch.setattr(type(settings), "to_projects_config", _failing_tpc)

    with pytest.raises(typer.Exit) as exc:
        run_doctor(
            load_settings_fn=lambda: (settings, Path("x")),
            telegram_checks=lambda *a: [],
            file_checks=_doctor_file_checks,
            voice_checks=_doctor_voice_checks,
        )
    assert exc.value.exit_code == 1


# ── run_doctor — mattermost with error checks ────────────────────────


def _mm_settings() -> TunapiSettings:
    return TunapiSettings.model_validate(
        {
            "transport": "mattermost",
            "transports": {
                "mattermost": {
                    "url": "http://mm",
                    "token": "tok",
                    "channel_id": "ch1",
                }
            },
        }
    )


def test_run_doctor_mattermost_with_error_check(monkeypatch) -> None:
    """Mattermost doctor with error checks exits 1."""
    settings = _mm_settings()
    monkeypatch.setattr(doctor_mod, "resolve_plugins_allowlist", lambda _s: None)
    monkeypatch.setattr(
        doctor_mod, "list_backend_ids", lambda allowlist=None: ["claude"]
    )

    async def _fake_mm(*_args, **_kwargs):
        return [DoctorCheck("mattermost token", "error", "bad token")]

    with pytest.raises(typer.Exit) as exc:
        run_doctor(
            load_settings_fn=lambda: (settings, Path("x")),
            telegram_checks=_fake_mm,
            file_checks=_doctor_file_checks,
            voice_checks=_doctor_voice_checks,
            mattermost_checks=_fake_mm,
        )
    assert exc.value.exit_code == 1


# ── run_doctor — telegram path ────────────────────────────────────────


def test_run_doctor_telegram_ok(monkeypatch) -> None:
    """Telegram doctor runs without error."""
    settings = _settings()
    monkeypatch.setattr(doctor_mod, "resolve_plugins_allowlist", lambda _s: None)
    monkeypatch.setattr(
        doctor_mod, "list_backend_ids", lambda allowlist=None: ["claude"]
    )

    async def _fake_tg(*_args, **_kwargs):
        return [DoctorCheck("telegram token", "ok", "@bot")]

    run_doctor(
        load_settings_fn=lambda: (settings, Path("x")),
        telegram_checks=_fake_tg,
        file_checks=_doctor_file_checks,
        voice_checks=_doctor_voice_checks,
    )


def test_run_doctor_telegram_missing_config(monkeypatch) -> None:
    """Telegram with no [transports.telegram] exits 1."""
    settings = TunapiSettings.model_validate(
        {"transport": "telegram", "transports": {}}
    )
    monkeypatch.setattr(doctor_mod, "resolve_plugins_allowlist", lambda _s: None)
    monkeypatch.setattr(
        doctor_mod, "list_backend_ids", lambda allowlist=None: ["claude"]
    )

    with pytest.raises(typer.Exit) as exc:
        run_doctor(
            load_settings_fn=lambda: (settings, Path("x")),
            telegram_checks=lambda *a: [],
            file_checks=_doctor_file_checks,
            voice_checks=_doctor_voice_checks,
        )
    assert exc.value.exit_code == 1


# ── _resolve_cli_attr (run module) ────────────────────────────────────


def test_run_resolve_cli_attr_missing_module(monkeypatch) -> None:
    """When tunapi.cli is not in sys.modules, returns None."""
    saved = sys.modules.get("tunapi.cli")
    try:
        sys.modules.pop("tunapi.cli", None)
        result = run_mod._resolve_cli_attr("anything")
        assert result is None
    finally:
        if saved is not None:
            sys.modules["tunapi.cli"] = saved
