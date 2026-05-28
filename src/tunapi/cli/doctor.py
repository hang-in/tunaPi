from __future__ import annotations

import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anyio
import typer

from ..config import ConfigError
from ..engines import list_backend_ids
from ..ids import RESERVED_CHAT_COMMANDS
from ..runtime_loader import resolve_plugins_allowlist
from ..settings import (
    MattermostTransportSettings,
    TunapiSettings,
    TelegramTopicsSettings,
    TelegramTransportSettings,
)
from ..mattermost.client import MattermostClient
from ..telegram.client import TelegramClient
from ..telegram.topics import _validate_topics_setup_for

DoctorStatus = Literal["ok", "warning", "error"]


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    label: str
    status: DoctorStatus
    detail: str | None = None
    suggestion: str | None = None

    def render(self) -> str:
        parts = [f"- {self.label}: {self.status}"]
        if self.detail:
            parts[0] += f" ({self.detail})"
        if self.suggestion:
            parts.append(f"  → {self.suggestion}")
        return "\n".join(parts)


def _classify_transport_error(
    exc: Exception,
    *,
    transport: str,
) -> tuple[str, str | None]:
    """예외를 사용자 친화적 메시지와 수정 가이드로 분류."""
    import httpx

    error_str = str(exc)

    if isinstance(exc, httpx.TimeoutException):
        return (
            f"timeout ({error_str[:80]})",
            f"{transport} 서버가 응답하지 않습니다. URL과 네트워크 연결을 확인하세요.",
        )

    if isinstance(exc, httpx.ConnectError):
        return (
            f"connection failed ({error_str[:80]})",
            f"{transport} 서버에 연결할 수 없습니다. URL, DNS, 방화벽을 확인하세요.",
        )

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return (
                f"auth failed (HTTP {status})",
                "토큰이 만료되었거나 잘못되었습니다. 토큰을 재발급하세요.",
            )
        return (
            f"HTTP {status} ({error_str[:80]})",
            None,
        )

    return (error_str[:120], None)


def _doctor_file_checks(settings: TelegramTransportSettings) -> list[DoctorCheck]:
    files = settings.files
    if not files.enabled:
        return [DoctorCheck("file transfer", "ok", "disabled")]
    if files.allowed_user_ids:
        count = len(files.allowed_user_ids)
        detail = f"restricted to {count} user id(s)"
        return [DoctorCheck("file transfer", "ok", detail)]
    return [DoctorCheck("file transfer", "warning", "enabled for all users")]


def _doctor_voice_checks(settings: TelegramTransportSettings) -> list[DoctorCheck]:
    if not settings.voice_transcription:
        return [DoctorCheck("voice transcription", "ok", "disabled")]
    api_key = settings.voice_transcription_api_key
    if api_key:
        return [
            DoctorCheck("voice transcription", "ok", "voice_transcription_api_key set")
        ]
    if os.environ.get("OPENAI_API_KEY"):
        return [DoctorCheck("voice transcription", "ok", "OPENAI_API_KEY set")]
    return [
        DoctorCheck(
            "voice transcription",
            "error",
            "API key not set",
            suggestion="OPENAI_API_KEY 환경변수를 설정하거나 voice_transcription_api_key를 tunapi.toml에 추가하세요.",
        )
    ]


async def _doctor_telegram_checks(
    token: str,
    chat_id: int,
    topics: TelegramTopicsSettings,
    project_chat_ids: tuple[int, ...],
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    client_factory = _resolve_cli_attr("TelegramClient") or TelegramClient
    validate_topics = (
        _resolve_cli_attr("_validate_topics_setup_for") or _validate_topics_setup_for
    )
    bot = client_factory(token)
    try:
        me = await bot.get_me()
        if me is None:
            checks.append(
                DoctorCheck(
                    "telegram token",
                    "error",
                    "failed to fetch bot info",
                    suggestion="TELEGRAM_TOKEN이 유효한지 BotFather에서 확인하세요.",
                )
            )
            checks.append(DoctorCheck("chat_id", "error", "skipped (token invalid)"))
            if topics.enabled:
                checks.append(DoctorCheck("topics", "error", "skipped (token invalid)"))
            else:
                checks.append(DoctorCheck("topics", "ok", "disabled"))
            return checks
        bot_label = f"@{me.username}" if me.username else f"id={me.id}"
        checks.append(DoctorCheck("telegram token", "ok", bot_label))
        chat = await bot.get_chat(chat_id)
        if chat is None:
            checks.append(DoctorCheck("chat_id", "error", f"unreachable ({chat_id})"))
        else:
            checks.append(DoctorCheck("chat_id", "ok", f"{chat.type} ({chat_id})"))
        if topics.enabled:
            try:
                await validate_topics(
                    bot=bot,
                    topics=topics,
                    chat_id=chat_id,
                    project_chat_ids=project_chat_ids,
                )
                checks.append(DoctorCheck("topics", "ok", f"scope={topics.scope}"))
            except ConfigError as exc:
                checks.append(DoctorCheck("topics", "error", str(exc)))
        else:
            checks.append(DoctorCheck("topics", "ok", "disabled"))
    except Exception as exc:  # noqa: BLE001
        detail, suggestion = _classify_transport_error(exc, transport="telegram")
        checks.append(DoctorCheck("telegram", "error", detail, suggestion=suggestion))
    finally:
        await bot.close()
    return checks


def _doctor_mm_file_checks(settings: MattermostTransportSettings) -> list[DoctorCheck]:
    files = settings.files
    if not files.enabled:
        return [DoctorCheck("file transfer", "ok", "disabled")]
    return [DoctorCheck("file transfer", "ok", "enabled")]


def _doctor_mm_voice_checks(settings: MattermostTransportSettings) -> list[DoctorCheck]:
    voice = settings.voice
    if not voice.enabled:
        return [DoctorCheck("voice transcription", "ok", "disabled")]
    if voice.api_key:
        return [DoctorCheck("voice transcription", "ok", "api_key set")]
    if os.environ.get("OPENAI_API_KEY"):
        return [DoctorCheck("voice transcription", "ok", "OPENAI_API_KEY set")]
    return [
        DoctorCheck(
            "voice transcription",
            "error",
            "API key not set",
            suggestion="OPENAI_API_KEY 환경변수를 설정하거나 voice_transcription_api_key를 tunapi.toml에 추가하세요.",
        )
    ]


async def _doctor_mattermost_checks(
    url: str,
    token: str,
    channel_id: str,
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    client_factory = _resolve_cli_attr("MattermostClient") or MattermostClient
    client = client_factory(url, token)
    try:
        me = await client.get_me()
        if me is None:
            checks.append(
                DoctorCheck("mattermost token", "error", "failed to fetch user info")
            )
            checks.append(DoctorCheck("channel_id", "error", "skipped (token invalid)"))
            return checks
        user_label = f"@{me.username}" if me.username else f"id={me.id}"
        checks.append(DoctorCheck("mattermost token", "ok", user_label))
        if channel_id:
            channel = await client.get_channel(channel_id)
            if channel is None:
                checks.append(
                    DoctorCheck("channel_id", "error", f"unreachable ({channel_id})")
                )
            else:
                name = getattr(channel, "display_name", None) or getattr(
                    channel, "name", channel_id
                )
                checks.append(DoctorCheck("channel_id", "ok", str(name)))
        else:
            checks.append(
                DoctorCheck("channel_id", "ok", "not set (using allowed_channel_ids)")
            )
    except Exception as exc:  # noqa: BLE001
        detail, suggestion = _classify_transport_error(exc, transport="mattermost")
        checks.append(DoctorCheck("mattermost", "error", detail, suggestion=suggestion))
    finally:
        await client.close()
    return checks


async def _doctor_slack_checks(
    bot_token: str,
    app_token: str,
    channel_id: str,
    allowed_channel_ids: tuple[str, ...] = (),
) -> list[DoctorCheck]:
    from ..slack.client import SlackClient
    from ..slack.client_api import HttpSlackClient

    checks: list[DoctorCheck] = []
    if not bot_token:
        checks.append(DoctorCheck("slack bot token", "error", "bot_token missing"))
        return checks
    if not app_token:
        checks.append(DoctorCheck("slack app token", "error", "app_token missing"))
        return checks

    client = SlackClient(bot_token, app_token)
    try:
        # 1. Bot token validation
        me = await client.auth_test()
        if not me.ok:
            checks.append(
                DoctorCheck("slack bot token", "error", f"invalid ({me.error})")
            )
            checks.append(DoctorCheck("channel_id", "error", "skipped (token invalid)"))
            checks.append(
                DoctorCheck("socket mode", "error", "skipped (token invalid)")
            )
            return checks
        user_label = f"@{me.user}" if me.user else f"id={me.user_id}"
        checks.append(DoctorCheck("slack bot token", "ok", user_label))

        # 2. Channel validation
        if channel_id:
            channel = await client.get_channel(channel_id)
            if channel is None:
                checks.append(
                    DoctorCheck("channel_id", "error", f"unreachable ({channel_id})")
                )
            else:
                name = getattr(channel, "name", channel_id)
                checks.append(DoctorCheck("channel_id", "ok", f"#{name}"))
        elif allowed_channel_ids:
            checks.append(
                DoctorCheck(
                    "channel_id",
                    "warning",
                    "channel_id not set — startup/restart notifications will be skipped",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "channel_id",
                    "error",
                    "channel_id not set and allowed_channel_ids is empty",
                )
            )

        # 3. Socket Mode readiness (app token + apps.connections.open)
        try:
            raw_client = HttpSlackClient(bot_token, app_token)
            try:
                url = await raw_client.apps_connections_open()
                if url:
                    checks.append(
                        DoctorCheck("socket mode", "ok", "connection available")
                    )
                else:
                    checks.append(
                        DoctorCheck(
                            "socket mode",
                            "error",
                            "apps.connections.open returned no URL "
                            "(check Socket Mode is enabled in Slack app settings)",
                        )
                    )
            finally:
                await raw_client.close()
        except Exception as exc:  # noqa: BLE001
            error_str = str(exc)
            if "invalid_auth" in error_str or "not_authed" in error_str:
                detail = "invalid app_token"
            elif "not_allowed_token_type" in error_str:
                detail = (
                    "app_token must be an xapp- token (Socket Mode app-level token)"
                )
            else:
                detail = error_str[:120]
            checks.append(DoctorCheck("socket mode", "error", detail))

    except Exception as exc:  # noqa: BLE001
        checks.append(DoctorCheck("slack", "error", str(exc)))
    finally:
        await client.close()
    return checks


def run_doctor(
    *,
    load_settings_fn: Callable[[], tuple[TunapiSettings, Path]],
    telegram_checks: Callable[
        [str, int, TelegramTopicsSettings, tuple[int, ...]],
        Awaitable[list[DoctorCheck]],
    ],
    file_checks: Callable[[TelegramTransportSettings], list[DoctorCheck]],
    voice_checks: Callable[[TelegramTransportSettings], list[DoctorCheck]],
    mattermost_checks: Callable[
        [str, str, str],
        Awaitable[list[DoctorCheck]],
    ] = _doctor_mattermost_checks,
    mm_file_checks: Callable[
        [MattermostTransportSettings], list[DoctorCheck]
    ] = _doctor_mm_file_checks,
    mm_voice_checks: Callable[
        [MattermostTransportSettings], list[DoctorCheck]
    ] = _doctor_mm_voice_checks,
    slack_checks: Callable[
        [str, str, str, tuple[str, ...]],
        Awaitable[list[DoctorCheck]],
    ] = _doctor_slack_checks,
) -> None:
    try:
        settings, config_path = load_settings_fn()
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if settings.transport not in ("telegram", "mattermost", "slack"):
        typer.echo(
            "error: tunapi doctor currently supports the telegram, mattermost and slack transports only.",
            err=True,
        )
        raise typer.Exit(code=1)

    allowlist = resolve_plugins_allowlist(settings)
    engine_ids = list_backend_ids(allowlist=allowlist)
    try:
        projects_cfg = settings.to_projects_config(
            config_path=config_path,
            engine_ids=engine_ids,
            reserved=RESERVED_CHAT_COMMANDS,
        )
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    checks: list[DoctorCheck] = []

    if settings.transport == "telegram":
        tg = settings.transports.telegram
        if tg is None:
            typer.echo(
                f"error: Missing [transports.telegram] in {config_path}.",
                err=True,
            )
            raise typer.Exit(code=1)

        project_chat_ids = projects_cfg.project_chat_ids()
        telegram_checks_result = anyio.run(
            telegram_checks,
            tg.bot_token,
            tg.chat_id,
            tg.topics,
            project_chat_ids,
        )
        if telegram_checks_result is None:
            telegram_checks_result = []
        checks = [
            *telegram_checks_result,
            *file_checks(tg),
            *voice_checks(tg),
        ]

    elif settings.transport == "mattermost":
        mm = settings.transports.mattermost
        if mm is None:
            typer.echo(
                f"error: Missing [transports.mattermost] in {config_path}.",
                err=True,
            )
            raise typer.Exit(code=1)

        mm_checks_result = anyio.run(
            mattermost_checks,
            mm.url,
            mm.token,
            mm.channel_id,
        )
        if mm_checks_result is None:
            mm_checks_result = []
        checks = [
            *mm_checks_result,
            *mm_file_checks(mm),
            *mm_voice_checks(mm),
        ]

    elif settings.transport == "slack":
        sl = settings.transports.slack
        if sl is None:
            typer.echo(
                f"error: Missing [transports.slack] in {config_path}.",
                err=True,
            )
            raise typer.Exit(code=1)

        checks = anyio.run(
            slack_checks,
            sl.bot_token,
            sl.app_token,
            sl.channel_id,
            tuple(sl.allowed_channel_ids),
        )

    typer.echo("tunapi doctor")
    for check in checks:
        typer.echo(check.render())
    if any(check.status == "error" for check in checks):
        raise typer.Exit(code=1)


def _resolve_cli_attr(name: str) -> object | None:
    cli_module = sys.modules.get("tunapi.cli")
    if cli_module is None:
        return None
    return getattr(cli_module, name, None)
