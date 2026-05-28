from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from tunapi.slack.parsing import SlackMessageEvent
from tunapi.transport import MessageRef


def _make_msg(
    text: str = "",
    channel_id: str = "C1",
    user_id: str = "U1",
    ts: str = "100.0",
    thread_ts: str | None = None,
    files: list | None = None,
) -> SlackMessageEvent:
    return SlackMessageEvent(
        channel_id=channel_id,
        user_id=user_id,
        text=text,
        ts=ts,
        thread_ts=thread_ts,
        files=files,
    )


def _make_cfg(
    *,
    files_enabled: bool = False,
    voice_enabled: bool = False,
    bot_user_id: str = "BOTU",
    channel_id: str | None = "C1",
    session_mode: str = "stateless",
    projects_root: str | None = None,
) -> MagicMock:
    cfg = MagicMock()
    cfg.files_enabled = files_enabled
    cfg.voice_enabled = voice_enabled
    cfg.bot_user_id = bot_user_id
    cfg.channel_id = channel_id
    cfg.session_mode = session_mode
    cfg.startup_msg = "Bot started"
    cfg.files_deny_globs = ()
    cfg.files_max_upload_bytes = 20 * 1024 * 1024
    cfg.files_max_download_bytes = 50 * 1024 * 1024
    cfg.voice_max_bytes = 10 * 1024 * 1024
    cfg.voice_model = "gpt-4o-mini-transcribe"
    cfg.voice_base_url = None
    cfg.voice_api_key = None
    cfg.projects_root = projects_root

    cfg.runtime = MagicMock()
    cfg.runtime.projects_root = projects_root
    cfg.runtime.default_engine = "claude"

    cfg.exec_cfg = MagicMock()
    cfg.exec_cfg.transport = AsyncMock()
    cfg.exec_cfg.transport.send = AsyncMock(
        return_value=MessageRef(channel_id="C1", message_id="200.0")
    )
    return cfg


def _make_resolved_message(
    prompt: str = "hello",
    engine_override: str | None = None,
    context: MagicMock | None = None,
) -> MagicMock:
    rm = MagicMock()
    rm.prompt = prompt
    rm.resume_token = None
    rm.engine_override = engine_override
    rm.context = context
    return rm


def _make_resolved_runner(*, issue: str | None = None) -> MagicMock:
    rr = MagicMock()
    rr.issue = issue
    rr.runner = MagicMock()
    return rr
