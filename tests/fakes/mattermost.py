from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from tunapi.mattermost.types import MattermostIncomingMessage
from tunapi.transport import MessageRef


def _make_msg(
    text: str = "",
    channel_id: str = "ch1",
    post_id: str = "p1",
    root_id: str = "",
    channel_type: str = "O",
    file_ids: tuple[str, ...] = (),
    sender_id: str = "u1",
    sender_username: str = "alice",
) -> MattermostIncomingMessage:
    return MattermostIncomingMessage(
        channel_id=channel_id,
        post_id=post_id,
        text=text,
        root_id=root_id,
        sender_id=sender_id,
        sender_username=sender_username,
        channel_type=channel_type,
        file_ids=file_ids,
    )


def _make_cfg(
    *,
    files_enabled: bool = False,
    voice_enabled: bool = False,
    bot_username: str = "tunabot",
    channel_id: str = "ch1",
    session_mode: str = "stateless",
    projects_root: str | None = None,
) -> MagicMock:
    cfg = MagicMock()
    cfg.files_enabled = files_enabled
    cfg.voice_enabled = voice_enabled
    cfg.bot_username = bot_username
    cfg.bot_user_id = "bot_uid"
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
        return_value=MessageRef(channel_id="ch1", message_id="200")
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
