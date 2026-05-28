"""Slack TransportBackend — entry point for the plugin system."""

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
from .bridge import SlackBridgeConfig, SlackPresenter, SlackTransport
from .client import SlackClient

logger = get_logger(__name__)


class SlackBackend(TransportBackend):
    id = "slack"
    description = "Slack bot (Socket Mode)"

    def check_setup(
        self,
        engine_backend: EngineBackend,
        *,
        transport_override: str | None = None,
    ) -> SetupResult:
        # Minimal check — full validation is in `tunapi doctor`
        from ..config import HOME_CONFIG_PATH

        return SetupResult(issues=[], config_path=HOME_CONFIG_PATH)

    async def interactive_setup(self, *, force: bool) -> bool:
        return False  # Not implemented for Slack

    def lock_token(self, *, transport_config: object, _config_path: Path) -> str | None:
        if isinstance(transport_config, dict):
            cfg = cast(dict[str, Any], transport_config)
            return cast(str | None, cfg.get("bot_token"))
        return cast(str | None, getattr(transport_config, "bot_token", None))

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
            raise TypeError("transport_config must be a dict for Slack")

        cfg = cast(dict[str, Any], transport_config)
        bot_token = cast(str, cfg.get("bot_token", ""))
        app_token = cast(str, cfg.get("app_token", ""))
        channel_id = cast(str | None, cfg.get("channel_id") or None)
        session_mode = cast(str, cfg.get("session_mode", "stateless"))
        show_resume_line = cast(bool, cfg.get("show_resume_line", True))
        message_overflow = cast(str, cfg.get("message_overflow", "trim"))
        allowed_channel_ids = tuple(cast(list[str], cfg.get("allowed_channel_ids", [])))
        allowed_user_ids = tuple(cast(list[str], cfg.get("allowed_user_ids", [])))
        trigger_mode = cast(str, cfg.get("trigger_mode", "mentions"))

        files_cfg = cfg.get("files", {})
        voice_cfg = cfg.get("voice", {})
        if not isinstance(files_cfg, dict):
            files_cfg = {}
        if not isinstance(voice_cfg, dict):
            voice_cfg = {}

        if not bot_token or not app_token:
            raise ValueError("Slack transport requires bot_token and app_token")

        startup_msg = build_startup_message(
            runtime,
            startup_pwd=os.getcwd(),
            session_mode=session_mode,
            show_resume_line=show_resume_line,
            bold="*",
            line_break="\n",
        )

        bot = SlackClient(bot_token, app_token)
        transport = SlackTransport(bot)
        presenter = SlackPresenter(
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
        runtime = p["runtime"]
        files_cfg = p["files_cfg"]
        voice_cfg = p["voice_cfg"]

        auth = await bot.auth_test()
        if not auth.ok:
            raise RuntimeError(f"Slack auth failed: {auth.error}")
        bot_user_id = auth.user_id or ""
        bot_username = auth.user or ""
        if not bot_user_id:
            logger.warning("slack.no_bot_user_id")

        logger.info(
            "slack.connected",
            bot_user_id=bot_user_id,
            bot_username=bot_username,
        )

        cfg = SlackBridgeConfig(
            bot=bot,
            bot_user_id=bot_user_id,
            bot_username=bot_username,
            runtime=runtime,
            channel_id=p["channel_id"],
            startup_msg=p["startup_msg"],
            exec_cfg=p["exec_cfg"],
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
                    "deny_globs",
                    [".git/**", ".env", ".envrc", "*.pem", ".ssh/**"],
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


slack_backend = SlackBackend()
BACKEND = slack_backend
