"""Mattermost TransportBackend — entry point for the plugin system."""

from __future__ import annotations

import os
from pathlib import Path

import anyio

from ..backends import EngineBackend
from ..logging import get_logger
from ..runner_bridge import ExecBridgeConfig
from ..transport_runtime import TransportRuntime
from ..transports import SetupResult, TransportBackend
from .bridge import MattermostBridgeConfig, MattermostPresenter, MattermostTransport
from .client import MattermostClient
from .onboarding import check_setup, interactive_setup

logger = get_logger(__name__)


def _build_startup_message(
    runtime: TransportRuntime,
    *,
    startup_pwd: str,
    session_mode: str,
    show_resume_line: bool,
) -> str:
    available_engines = list(runtime.available_engine_ids())
    missing_engines = list(runtime.missing_engine_ids())

    engine_list = ", ".join(available_engines) if available_engines else "none"
    notes: list[str] = []
    if missing_engines:
        notes.append(f"not installed: {', '.join(missing_engines)}")
    misconfigured = list(runtime.engine_ids_with_status("bad_config"))
    if misconfigured:
        notes.append(f"misconfigured: {', '.join(misconfigured)}")
    if notes:
        engine_list = f"{engine_list} ({'; '.join(notes)})"

    project_aliases = sorted(set(runtime.project_aliases()), key=str.lower)
    project_list = ", ".join(project_aliases) if project_aliases else "none"
    resume_label = "shown" if show_resume_line else "hidden"

    return (
        f"**tunapi is ready**\n\n"
        f"default: `{runtime.default_engine}`  \n"
        f"engines: `{engine_list}`  \n"
        f"projects: `{project_list}`  \n"
        f"mode: `{session_mode}`  \n"
        f"resume lines: `{resume_label}`  \n"
        f"working in: `{startup_pwd}`"
    )


class MattermostBackend(TransportBackend):
    id = "mattermost"
    description = "Mattermost bot"

    def check_setup(
        self,
        engine_backend: EngineBackend,
        *,
        transport_override: str | None = None,
    ) -> SetupResult:
        return check_setup(engine_backend, transport_override=transport_override)

    async def interactive_setup(self, *, force: bool) -> bool:
        return await interactive_setup(force=force)

    def lock_token(
        self, *, transport_config: object, _config_path: Path
    ) -> str | None:
        if isinstance(transport_config, dict):
            return transport_config.get("token")
        return getattr(transport_config, "token", None)

    def build_and_run(
        self,
        *,
        transport_config: object,
        config_path: Path,
        runtime: TransportRuntime,
        final_notify: bool,
        default_engine_override: str | None,
    ) -> None:
        if not isinstance(transport_config, dict):
            raise TypeError("transport_config must be a dict for Mattermost")

        url = transport_config.get("url", "")
        token = transport_config.get("token", "")
        channel_id = transport_config.get("channel_id", "")
        session_mode = transport_config.get("session_mode", "stateless")
        show_resume_line = transport_config.get("show_resume_line", True)
        message_overflow = transport_config.get("message_overflow", "trim")
        allowed_channel_ids = tuple(transport_config.get("allowed_channel_ids", []))
        allowed_user_ids = tuple(transport_config.get("allowed_user_ids", []))
        trigger_mode = transport_config.get("trigger_mode", "all")

        files_cfg = transport_config.get("files", {})
        voice_cfg = transport_config.get("voice", {})

        startup_msg = _build_startup_message(
            runtime,
            startup_pwd=os.getcwd(),
            session_mode=session_mode,
            show_resume_line=show_resume_line,
        )

        bot = MattermostClient(url, token)
        transport = MattermostTransport(bot)
        presenter = MattermostPresenter(
            message_overflow=message_overflow,
            show_resume_line=show_resume_line,
        )
        exec_cfg = ExecBridgeConfig(
            transport=transport,
            presenter=presenter,
            final_notify=final_notify,
        )

        async def run_loop() -> None:
            me = await bot.get_me()
            bot_user_id = me.id if me else ""
            bot_username = me.username if me else ""
            if not bot_user_id:
                logger.warning("mattermost.no_bot_user_id")

            cfg = MattermostBridgeConfig(
                bot=bot,
                bot_user_id=bot_user_id,
                bot_username=bot_username,
                runtime=runtime,
                channel_id=channel_id,
                startup_msg=startup_msg,
                exec_cfg=exec_cfg,
                session_mode=session_mode,
                show_resume_line=show_resume_line,
                allowed_channel_ids=allowed_channel_ids,
                allowed_user_ids=allowed_user_ids,
                message_overflow=message_overflow,
                trigger_mode=trigger_mode,
                files_enabled=files_cfg.get("enabled", False),
                files_uploads_dir=files_cfg.get("uploads_dir", "incoming"),
                files_deny_globs=tuple(files_cfg.get("deny_globs", [".git/**", ".env", ".envrc", "*.pem", ".ssh/**"])),
                files_max_upload_bytes=files_cfg.get("max_upload_bytes", 20 * 1024 * 1024),
                files_max_download_bytes=files_cfg.get("max_download_bytes", 50 * 1024 * 1024),
                voice_enabled=voice_cfg.get("enabled", False),
                voice_max_bytes=voice_cfg.get("max_bytes", 10 * 1024 * 1024),
                voice_model=voice_cfg.get("model", "gpt-4o-mini-transcribe"),
                voice_base_url=voice_cfg.get("base_url"),
                voice_api_key=voice_cfg.get("api_key"),
                projects_root=runtime.projects_root,
            )

            from .loop import run_main_loop

            try:
                await run_main_loop(
                    cfg,
                    watch_config=runtime.watch_config,
                    default_engine_override=default_engine_override,
                    transport_id=self.id,
                    transport_config=transport_config,
                )
            finally:
                await transport.close()

        anyio.run(run_loop)


mattermost_backend = MattermostBackend()
BACKEND = mattermost_backend
