"""Extra tests for tunapi.cli.doctor — MM/Slack file/voice checks, run_doctor paths."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import typer
from typer.testing import CliRunner

import importlib

from tunapi import cli

doctor_mod = importlib.import_module("tunapi.cli.doctor")

from tunapi.cli.doctor import (
    DoctorCheck,
    _doctor_file_checks,
    _doctor_mm_file_checks,
    _doctor_mm_voice_checks,
    _doctor_voice_checks,
    _doctor_mattermost_checks,
    _resolve_cli_attr,
    run_doctor,
)
from tunapi.config import ConfigError
from tunapi.settings import (
    MattermostFilesSettings,
    MattermostTransportSettings,
    MattermostVoiceSettings,
    TelegramTransportSettings,
    TunapiSettings,
)


# ── DoctorCheck.render ──────────────────────────────────────────────


def test_render_with_detail() -> None:
    c = DoctorCheck("label", "ok", "some detail")
    assert c.render() == "- label: ok (some detail)"


def test_render_without_detail() -> None:
    c = DoctorCheck("label", "warning")
    assert c.render() == "- label: warning"


# ── _doctor_file_checks (telegram) ──────────────────────────────────


def test_tg_file_checks_disabled() -> None:
    s = TelegramTransportSettings.model_validate(
        {"bot_token": "t", "chat_id": 1, "files": {"enabled": False}}
    )
    checks = _doctor_file_checks(s)
    assert len(checks) == 1
    assert checks[0].detail == "disabled"


def test_tg_file_checks_restricted() -> None:
    s = TelegramTransportSettings.model_validate(
        {
            "bot_token": "t",
            "chat_id": 1,
            "files": {"enabled": True, "allowed_user_ids": [111, 222]},
        }
    )
    checks = _doctor_file_checks(s)
    assert "restricted to 2 user id(s)" in (checks[0].detail or "")


def test_tg_file_checks_open() -> None:
    s = TelegramTransportSettings.model_validate(
        {"bot_token": "t", "chat_id": 1, "files": {"enabled": True}}
    )
    checks = _doctor_file_checks(s)
    assert checks[0].status == "warning"
    assert "enabled for all users" in (checks[0].detail or "")


# ── _doctor_voice_checks (telegram) ─────────────────────────────────


def test_tg_voice_disabled() -> None:
    s = TelegramTransportSettings.model_validate(
        {"bot_token": "t", "chat_id": 1, "voice_transcription": False}
    )
    checks = _doctor_voice_checks(s)
    assert checks[0].detail == "disabled"


def test_tg_voice_api_key_set() -> None:
    s = TelegramTransportSettings.model_validate(
        {
            "bot_token": "t",
            "chat_id": 1,
            "voice_transcription": True,
            "voice_transcription_api_key": "sk-xxx",
        }
    )
    checks = _doctor_voice_checks(s)
    assert checks[0].status == "ok"
    assert "voice_transcription_api_key set" in (checks[0].detail or "")


def test_tg_voice_env_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    s = TelegramTransportSettings.model_validate(
        {"bot_token": "t", "chat_id": 1, "voice_transcription": True}
    )
    checks = _doctor_voice_checks(s)
    assert checks[0].status == "ok"
    assert "OPENAI_API_KEY set" in (checks[0].detail or "")


def test_tg_voice_no_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    s = TelegramTransportSettings.model_validate(
        {"bot_token": "t", "chat_id": 1, "voice_transcription": True}
    )
    checks = _doctor_voice_checks(s)
    assert checks[0].status == "error"
    assert "API key not set" in (checks[0].detail or "")


# ── _doctor_mm_file_checks ─────────────────────────────────────────


def test_mm_file_disabled() -> None:
    s = MattermostTransportSettings.model_validate(
        {"url": "http://mm", "token": "t", "files": {"enabled": False}}
    )
    checks = _doctor_mm_file_checks(s)
    assert checks[0].detail == "disabled"


def test_mm_file_enabled() -> None:
    s = MattermostTransportSettings.model_validate(
        {"url": "http://mm", "token": "t", "files": {"enabled": True}}
    )
    checks = _doctor_mm_file_checks(s)
    assert checks[0].status == "ok"
    assert checks[0].detail == "enabled"


# ── _doctor_mm_voice_checks ────────────────────────────────────────


def test_mm_voice_disabled() -> None:
    s = MattermostTransportSettings.model_validate(
        {"url": "http://mm", "token": "t", "voice": {"enabled": False}}
    )
    checks = _doctor_mm_voice_checks(s)
    assert checks[0].detail == "disabled"


def test_mm_voice_api_key_set() -> None:
    s = MattermostTransportSettings.model_validate(
        {"url": "http://mm", "token": "t", "voice": {"enabled": True, "api_key": "k"}}
    )
    checks = _doctor_mm_voice_checks(s)
    assert checks[0].status == "ok"
    assert "api_key set" in (checks[0].detail or "")


def test_mm_voice_env_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    s = MattermostTransportSettings.model_validate(
        {"url": "http://mm", "token": "t", "voice": {"enabled": True}}
    )
    checks = _doctor_mm_voice_checks(s)
    assert checks[0].status == "ok"


def test_mm_voice_no_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    s = MattermostTransportSettings.model_validate(
        {"url": "http://mm", "token": "t", "voice": {"enabled": True}}
    )
    checks = _doctor_mm_voice_checks(s)
    assert checks[0].status == "error"


# ── _doctor_mattermost_checks ──────────────────────────────────────


class _FakeMmUser:
    def __init__(self, id: str, username: str) -> None:
        self.id = id
        self.username = username


class _FakeMmChannel:
    def __init__(self, name: str) -> None:
        self.name = name
        self.display_name = name


class _FakeMmClient:
    def __init__(
        self, me: _FakeMmUser | None = None, channel: _FakeMmChannel | None = None
    ) -> None:
        self._me = me
        self._channel = channel
        self.closed = False

    async def get_me(self) -> _FakeMmUser | None:
        return self._me

    async def get_channel(self, channel_id: str) -> _FakeMmChannel | None:
        return self._channel

    async def close(self) -> None:
        self.closed = True


@pytest.mark.anyio
async def test_mm_checks_ok(monkeypatch) -> None:
    client = _FakeMmClient(
        me=_FakeMmUser("u1", "botuser"),
        channel=_FakeMmChannel("general"),
    )
    monkeypatch.setattr(doctor_mod, "MattermostClient", lambda _url, _token: client)
    checks = await _doctor_mattermost_checks("http://mm", "token", "ch1")
    assert checks[0].status == "ok"
    assert "@botuser" in (checks[0].detail or "")
    assert checks[1].status == "ok"
    assert "general" in (checks[1].detail or "")
    assert client.closed


@pytest.mark.anyio
async def test_mm_checks_invalid_token(monkeypatch) -> None:
    client = _FakeMmClient(me=None)
    monkeypatch.setattr(doctor_mod, "MattermostClient", lambda _url, _token: client)
    checks = await _doctor_mattermost_checks("http://mm", "token", "ch1")
    assert checks[0].status == "error"
    assert checks[1].status == "error"
    assert "skipped" in (checks[1].detail or "")


@pytest.mark.anyio
async def test_mm_checks_channel_unreachable(monkeypatch) -> None:
    client = _FakeMmClient(me=_FakeMmUser("u1", "bot"), channel=None)
    monkeypatch.setattr(doctor_mod, "MattermostClient", lambda _url, _token: client)
    checks = await _doctor_mattermost_checks("http://mm", "token", "ch1")
    assert checks[1].status == "error"
    assert "unreachable" in (checks[1].detail or "")


@pytest.mark.anyio
async def test_mm_checks_no_channel_id(monkeypatch) -> None:
    client = _FakeMmClient(me=_FakeMmUser("u1", "bot"))
    monkeypatch.setattr(doctor_mod, "MattermostClient", lambda _url, _token: client)
    checks = await _doctor_mattermost_checks("http://mm", "token", "")
    assert checks[0].status == "ok"
    assert checks[1].status == "ok"
    assert "allowed_channel_ids" in (checks[1].detail or "")


@pytest.mark.anyio
async def test_mm_checks_exception(monkeypatch) -> None:
    class _BrokenClient:
        async def get_me(self):
            raise RuntimeError("connection failed")

        async def close(self):
            pass

    monkeypatch.setattr(doctor_mod, "MattermostClient", lambda _u, _t: _BrokenClient())
    checks = await _doctor_mattermost_checks("http://mm", "token", "ch1")
    assert checks[0].status == "error"
    assert "connection failed" in (checks[0].detail or "")


# ── run_doctor for mattermost transport ─────────────────────────────


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


def test_run_doctor_mattermost_ok(monkeypatch) -> None:
    settings = _mm_settings()
    monkeypatch.setattr(doctor_mod, "resolve_plugins_allowlist", lambda _s: None)
    monkeypatch.setattr(doctor_mod, "list_backend_ids", lambda allowlist=None: ["claude"])

    async def _fake_mm(*_args, **_kwargs):
        return [DoctorCheck("mattermost token", "ok", "@bot")]

    # No error checks → should return without raising
    run_doctor(
        load_settings_fn=lambda: (settings, Path("x")),
        telegram_checks=_fake_mm,
        file_checks=_doctor_file_checks,
        voice_checks=_doctor_voice_checks,
        mattermost_checks=_fake_mm,
    )


def test_run_doctor_mattermost_missing_config(monkeypatch) -> None:
    settings = TunapiSettings.model_validate(
        {"transport": "mattermost", "transports": {}}
    )
    monkeypatch.setattr(doctor_mod, "resolve_plugins_allowlist", lambda _s: None)
    monkeypatch.setattr(doctor_mod, "list_backend_ids", lambda allowlist=None: ["claude"])

    with pytest.raises(typer.Exit) as exc_info:
        run_doctor(
            load_settings_fn=lambda: (settings, Path("x")),
            telegram_checks=lambda *a: [],
            file_checks=_doctor_file_checks,
            voice_checks=_doctor_voice_checks,
        )
    assert exc_info.value.exit_code == 1


# ── run_doctor for slack transport ──────────────────────────────────


def _slack_settings() -> TunapiSettings:
    return TunapiSettings.model_validate(
        {
            "transport": "slack",
            "transports": {
                "slack": {
                    "bot_token": "xoxb-test",
                    "app_token": "xapp-test",
                    "channel_id": "C123",
                }
            },
        }
    )


def test_run_doctor_slack_ok(monkeypatch) -> None:
    settings = _slack_settings()
    monkeypatch.setattr(doctor_mod, "resolve_plugins_allowlist", lambda _s: None)
    monkeypatch.setattr(doctor_mod, "list_backend_ids", lambda allowlist=None: ["claude"])

    async def _fake_slack(*_args, **_kwargs):
        return [DoctorCheck("slack bot token", "ok", "@bot")]

    run_doctor(
        load_settings_fn=lambda: (settings, Path("x")),
        telegram_checks=_fake_slack,
        file_checks=_doctor_file_checks,
        voice_checks=_doctor_voice_checks,
        slack_checks=_fake_slack,
    )


def test_run_doctor_slack_missing_config(monkeypatch) -> None:
    settings = TunapiSettings.model_validate(
        {"transport": "slack", "transports": {}}
    )
    monkeypatch.setattr(doctor_mod, "resolve_plugins_allowlist", lambda _s: None)
    monkeypatch.setattr(doctor_mod, "list_backend_ids", lambda allowlist=None: ["claude"])

    with pytest.raises(typer.Exit) as exc_info:
        run_doctor(
            load_settings_fn=lambda: (settings, Path("x")),
            telegram_checks=lambda *a: [],
            file_checks=_doctor_file_checks,
            voice_checks=_doctor_voice_checks,
        )
    assert exc_info.value.exit_code == 1


# ── run_doctor unsupported transport ────────────────────────────────


def test_run_doctor_unsupported_transport(monkeypatch) -> None:
    settings = TunapiSettings.model_validate(
        {"transport": "tunadish", "transports": {}}
    )
    monkeypatch.setattr(doctor_mod, "resolve_plugins_allowlist", lambda _s: None)
    monkeypatch.setattr(doctor_mod, "list_backend_ids", lambda allowlist=None: ["claude"])

    with pytest.raises(typer.Exit) as exc_info:
        run_doctor(
            load_settings_fn=lambda: (settings, Path("x")),
            telegram_checks=lambda *a: [],
            file_checks=_doctor_file_checks,
            voice_checks=_doctor_voice_checks,
        )
    assert exc_info.value.exit_code == 1


# ── run_doctor config error ─────────────────────────────────────────


def test_run_doctor_config_error() -> None:
    def _raise():
        raise ConfigError("bad config")

    with pytest.raises(typer.Exit) as exc_info:
        run_doctor(
            load_settings_fn=_raise,
            telegram_checks=lambda *a: [],
            file_checks=_doctor_file_checks,
            voice_checks=_doctor_voice_checks,
        )
    assert exc_info.value.exit_code == 1


# ── _resolve_cli_attr ───────────────────────────────────────────────


def test_resolve_cli_attr_returns_none_for_missing() -> None:
    assert _resolve_cli_attr("nonexistent_attr_xyz") is None


def test_resolve_cli_attr_returns_value(monkeypatch) -> None:
    import tunapi.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_test_sentinel", "found", raising=False)
    # Ensure module is in sys.modules
    import sys
    assert "tunapi.cli" in sys.modules
    result = _resolve_cli_attr("_test_sentinel")
    assert result == "found"
