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

    def render(self) -> str:
        if self.detail:
            return f"- {self.label}: {self.status} ({self.detail})"
        return f"- {self.label}: {self.status}"


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
    return [DoctorCheck("voice transcription", "error", "API key not set")]


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
                DoctorCheck("telegram token", "error", "failed to fetch bot info")
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
        checks.append(DoctorCheck("telegram", "error", str(exc)))
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
    return [DoctorCheck("voice transcription", "error", "API key not set")]


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
            checks.append(
                DoctorCheck("channel_id", "error", "skipped (token invalid)")
            )
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
                name = getattr(channel, "display_name", None) or getattr(channel, "name", channel_id)
                checks.append(DoctorCheck("channel_id", "ok", str(name)))
        else:
            checks.append(DoctorCheck("channel_id", "ok", "not set (using allowed_channel_ids)"))
    except Exception as exc:  # noqa: BLE001
        checks.append(DoctorCheck("mattermost", "error", str(exc)))
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
) -> None:
    try:
        settings, config_path = load_settings_fn()
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if settings.transport not in ("telegram", "mattermost"):
        typer.echo(
            "error: tunapi doctor currently supports the telegram and mattermost transports only.",
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
