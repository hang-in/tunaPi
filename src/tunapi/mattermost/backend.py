"""Mattermost TransportBackend — entry point for the plugin system."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import anyio

from ..backends import EngineBackend
from ..core.startup import build_startup_message
from ..logging import get_logger
from ..runner_bridge import ExecBridgeConfig
from ..transport_runtime import TransportRuntime
from ..transports import SetupResult, TransportBackend
from .bridge import MattermostBridgeConfig, MattermostPresenter, MattermostTransport
from .client import MattermostClient
from .onboarding import check_setup, interactive_setup

logger = get_logger(__name__)


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

    def lock_token(self, *, transport_config: object, _config_path: Path) -> str | None:
        if isinstance(transport_config, dict):
            cfg = cast(dict[str, Any], transport_config)
            return cast(str | None, cfg.get("token"))
        return cast(str | None, getattr(transport_config, "token", None))

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

        cfg = cast(dict[str, Any], transport_config)
        url = cast(str, cfg.get("url", ""))
        token = cast(str, cfg.get("token", ""))
        channel_id = cast(str, cfg.get("channel_id", ""))
        session_mode = cast(str, cfg.get("session_mode", "stateless"))
        show_resume_line = cast(bool, cfg.get("show_resume_line", True))
        message_overflow = cast(str, cfg.get("message_overflow", "trim"))
        allowed_channel_ids = tuple(cast(list[str], cfg.get("allowed_channel_ids", [])))
        allowed_user_ids = tuple(cast(list[str], cfg.get("allowed_user_ids", [])))
        trigger_mode = cast(str, cfg.get("trigger_mode", "all"))

        files_cfg = cfg.get("files", {})
        voice_cfg = cfg.get("voice", {})
        if not isinstance(files_cfg, dict):
            files_cfg = {}
        if not isinstance(voice_cfg, dict):
            voice_cfg = {}

        startup_msg = build_startup_message(
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

        self._pending_run = {
            "bot": bot,
            "transport": transport,
            "exec_cfg": exec_cfg,
            "runtime": runtime,
            "channel_id": channel_id,
            "startup_msg": startup_msg,
            "session_mode": session_mode,
            "show_resume_line": show_resume_line,
            "allowed_channel_ids": allowed_channel_ids,
            "allowed_user_ids": allowed_user_ids,
            "message_overflow": message_overflow,
            "trigger_mode": trigger_mode,
            "files_cfg": files_cfg,
            "voice_cfg": voice_cfg,
            "transport_config": cfg,
            "default_engine_override": default_engine_override,
        }
        self._prepared = True
        if not self._prepare_only:
            anyio.run(self.async_run)

    _prepared: bool = False
    _prepare_only: bool = False
    _pending_run: dict[str, Any] = {}

    async def async_run(self) -> None:
        """Async entry point — can be called from a shared task group."""
        p = self._pending_run
        bot = p["bot"]
        transport = p["transport"]
        exec_cfg = p["exec_cfg"]
        runtime = p["runtime"]
        files_cfg = p["files_cfg"]
        voice_cfg = p["voice_cfg"]

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
            channel_id=p["channel_id"],
            startup_msg=p["startup_msg"],
            exec_cfg=exec_cfg,
            session_mode=p["session_mode"],
            show_resume_line=p["show_resume_line"],
            allowed_channel_ids=p["allowed_channel_ids"],
            allowed_user_ids=p["allowed_user_ids"],
            message_overflow=p["message_overflow"],
            trigger_mode=p["trigger_mode"],
            files_enabled=files_cfg.get("enabled", False),
            files_uploads_dir=files_cfg.get("uploads_dir", "incoming"),
            files_deny_globs=tuple(
                files_cfg.get(
                    "deny_globs", [".git/**", ".env", ".envrc", "*.pem", ".ssh/**"]
                )
            ),
            files_max_upload_bytes=files_cfg.get("max_upload_bytes", 20 * 1024 * 1024),
            files_max_download_bytes=files_cfg.get(
                "max_download_bytes", 50 * 1024 * 1024
            ),
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
                default_engine_override=p["default_engine_override"],
                transport_id=self.id,
                transport_config=p["transport_config"],
            )
        finally:
            await transport.close()


mattermost_backend = MattermostBackend()
BACKEND = mattermost_backend
