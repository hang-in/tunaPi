"""Discord transport backend for tunapi."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, cast

import anyio

from tunapi.backends import EngineBackend
from tunapi.logging import get_logger
from tunapi.runner_bridge import ExecBridgeConfig
from tunapi.transport_runtime import TransportRuntime
from tunapi.transports import SetupResult, TransportBackend

from .bridge import (
    DiscordBridgeConfig,
    DiscordFilesSettings,
    DiscordPresenter,
    DiscordTransport,
    DiscordVoiceMessageSettings,
)
from .client import DiscordBotClient
from .loop import run_main_loop
from .onboarding import check_setup, interactive_setup
from .allowlist import normalize_user_id_set

logger = get_logger(__name__)

__all__ = ["BACKEND", "DiscordBackend"]


def _get_discord_settings(transport_config: object) -> dict[str, Any]:
    """Extract Discord settings from transport config.

    Since Discord is a plugin, settings come as a dict from model_extra.
    """
    if isinstance(transport_config, dict):
        return transport_config
    # Try to convert pydantic model to dict
    if hasattr(transport_config, "model_dump"):
        return transport_config.model_dump()
    raise TypeError(f"unexpected transport_config type: {type(transport_config)}")


def _build_startup_message(
    runtime: TransportRuntime,
    *,
    startup_pwd: str,
) -> str:
    """Build the startup message displayed when bot connects."""
    available_engines = list(runtime.available_engine_ids())
    missing_engines = list(runtime.missing_engine_ids())
    misconfigured_engines = list(runtime.engine_ids_with_status("bad_config"))
    failed_engines = list(runtime.engine_ids_with_status("load_error"))

    engine_list = ", ".join(available_engines) if available_engines else "none"

    notes: list[str] = []
    if missing_engines:
        notes.append(f"not installed: {', '.join(missing_engines)}")
    if misconfigured_engines:
        notes.append(f"misconfigured: {', '.join(misconfigured_engines)}")
    if failed_engines:
        notes.append(f"failed to load: {', '.join(failed_engines)}")
    if notes:
        engine_list = f"{engine_list} ({'; '.join(notes)})"

    project_aliases = sorted(set(runtime.project_aliases()), key=str.lower)
    project_list = ", ".join(project_aliases) if project_aliases else "none"

    return (
        f"\N{OCTOPUS} **tunapi-discord is ready**\n\n"
        f"default: `{runtime.default_engine}`  \n"
        f"agents: `{engine_list}`  \n"
        f"projects: `{project_list}`  \n"
        f"working in: `{startup_pwd}`"
    )


class DiscordBackend(TransportBackend):
    """Discord transport backend implementation."""

    id = "discord"
    description = "Discord bot"

    def check_setup(
        self,
        engine_backend: EngineBackend,
        *,
        transport_override: str | None = None,
    ) -> SetupResult:
        return check_setup(engine_backend, transport_override=transport_override)

    async def interactive_setup(self, *, force: bool) -> bool:
        return await interactive_setup(force=force)

    def lock_token(self, *, transport_config: object, _config_path: Path) -> str | None:
        settings = _get_discord_settings(transport_config)
        return settings.get("bot_token")

    def build_and_run(
        self,
        *,
        transport_config: object,
        config_path: Path,
        runtime: TransportRuntime,
        final_notify: bool,
        default_engine_override: str | None,
    ) -> None:
        settings = _get_discord_settings(transport_config)
        token = settings["bot_token"]
        guild_id = settings.get("guild_id")
        message_overflow = settings.get("message_overflow", "split")
        session_mode = settings.get("session_mode", "stateless")
        show_resume_line = settings.get("show_resume_line", True)
        trigger_mode_default_raw = settings.get("trigger_mode_default", "all")
        trigger_mode_default_normalized: str | None = None
        if isinstance(trigger_mode_default_raw, str):
            trigger_mode_default_normalized = trigger_mode_default_raw.strip().lower()
        if trigger_mode_default_normalized not in {"all", "mentions"}:
            logger.warning(
                "config.invalid_trigger_mode_default",
                value=trigger_mode_default_raw,
                fallback="all",
            )
            trigger_mode_default_normalized = "all"
        trigger_mode_default = cast(
            Literal["all", "mentions"], trigger_mode_default_normalized
        )

        allowed_user_ids_raw = settings.get("allowed_user_ids")
        allowed_user_ids = normalize_user_id_set(allowed_user_ids_raw)
        if allowed_user_ids_raw and allowed_user_ids is None:
            logger.warning(
                "config.invalid_allowed_user_ids",
                value=allowed_user_ids_raw,
            )

        media_group_debounce_s_raw = settings.get("media_group_debounce_s", 0.75)
        try:
            media_group_debounce_s = float(media_group_debounce_s_raw)
        except (TypeError, ValueError):
            logger.warning(
                "config.invalid_media_group_debounce_s",
                value=media_group_debounce_s_raw,
                fallback=0.75,
            )
            media_group_debounce_s = 0.75
        if media_group_debounce_s < 0:
            logger.warning(
                "config.invalid_media_group_debounce_s",
                value=media_group_debounce_s_raw,
                fallback=0.75,
            )
            media_group_debounce_s = 0.75

        # Parse files settings
        files_settings = settings.get("files", {})
        files_allowed_user_ids_raw = files_settings.get("allowed_user_ids")
        files_allowed_user_ids = normalize_user_id_set(files_allowed_user_ids_raw)
        if files_allowed_user_ids_raw and files_allowed_user_ids is None:
            logger.warning(
                "config.invalid_files_allowed_user_ids",
                value=files_allowed_user_ids_raw,
            )
        files_config = DiscordFilesSettings(
            enabled=files_settings.get("enabled", False),
            auto_put=files_settings.get("auto_put", True),
            auto_put_mode=files_settings.get("auto_put_mode", "upload"),
            uploads_dir=files_settings.get("uploads_dir", "incoming"),
            max_upload_bytes=files_settings.get("max_upload_bytes", 20 * 1024 * 1024),
            deny_globs=tuple(
                files_settings.get(
                    "deny_globs",
                    [".git/**", ".env", ".envrc", "**/*.pem", "**/.ssh/**"],
                )
            ),
            allowed_user_ids=files_allowed_user_ids,
        )

        # Parse voice message transcription settings
        voice_settings = settings.get("voice_messages", {})
        voice_messages_config = DiscordVoiceMessageSettings(
            enabled=voice_settings.get("enabled", False),
            max_bytes=voice_settings.get("max_bytes", 10 * 1024 * 1024),
            whisper_model=voice_settings.get("whisper_model", "base"),
        )

        startup_msg = _build_startup_message(
            runtime,
            startup_pwd=os.getcwd(),
        )

        bot = DiscordBotClient(token, guild_id=guild_id)
        transport = DiscordTransport(bot)
        presenter = DiscordPresenter(message_overflow=message_overflow)
        exec_cfg = ExecBridgeConfig(
            transport=transport,
            presenter=presenter,
            final_notify=final_notify,
        )
        cfg = DiscordBridgeConfig(
            bot=bot,
            runtime=runtime,
            guild_id=guild_id,
            allowed_user_ids=allowed_user_ids,
            startup_msg=startup_msg,
            exec_cfg=exec_cfg,
            session_mode=session_mode,
            show_resume_line=show_resume_line,
            message_overflow=message_overflow,
            trigger_mode_default=trigger_mode_default,
            files=files_config,
            media_group_debounce_s=media_group_debounce_s,
            voice_messages=voice_messages_config,
        )

        async def run_loop() -> None:
            await run_main_loop(
                cfg,
                default_engine_override=default_engine_override,
                config_path=config_path if runtime.watch_config else None,
                transport_config=settings,
            )

        anyio.run(run_loop)


discord_backend = DiscordBackend()
BACKEND = discord_backend
