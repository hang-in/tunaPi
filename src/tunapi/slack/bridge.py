"""Slack Transport and Presenter implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..core.presenter import ChatPresenter
from ..logging import get_logger
from ..markdown import MarkdownFormatter
from ..runner_bridge import ExecBridgeConfig
from ..transport import MessageRef, RenderedMessage, SendOptions
from ..transport_runtime import TransportRuntime
from .client import SlackClient
from .render import prepare_slack, prepare_slack_multi

logger = get_logger(__name__)

# Slack cancel via 🛑 reaction
CANCEL_EMOJI = "octagonal_sign"


class SlackPresenter(ChatPresenter):
    """Renders :class:`ProgressState` into Slack mrkdwn messages."""

    def __init__(
        self,
        *,
        formatter: MarkdownFormatter | None = None,
        message_overflow: str = "trim",
        show_resume_line: bool = True,
    ) -> None:
        super().__init__(
            prepare=prepare_slack,
            prepare_multi=prepare_slack_multi,
            formatter=formatter,
            message_overflow=message_overflow,
            show_resume_line=show_resume_line,
        )


class SlackTransport:
    """Implements the :class:`Transport` protocol for Slack."""

    def __init__(self, bot: SlackClient) -> None:
        self._bot = bot

    async def send(
        self,
        *,
        channel_id: str | int,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef | None:
        ch = str(channel_id)
        thread_ts: str | None = None
        if options is not None and options.thread_id is not None:
            thread_ts = str(options.thread_id)

        resp = await self._bot.send_message(
            ch,
            message.text,
            thread_ts=thread_ts,
        )
        if resp is None or not resp.ok:
            return None
        if resp.ts is None:
            return None

        thread_id = resp.message.thread_ts if resp.message else None
        thread_id = thread_id or resp.ts
        ref = MessageRef(
            channel_id=ch,
            message_id=resp.ts,
            raw=resp.message,
            thread_id=thread_id,
            sender_id=resp.message.user if resp.message else None,
        )

        followups = message.extra.get("followups") if message.extra else None
        if followups:
            await self._send_followups(
                followups,
                channel_id=ch,
                thread_ts=thread_id,
            )

        return ref

    async def edit(
        self,
        *,
        ref: MessageRef,
        message: RenderedMessage,
        wait: bool = True,
    ) -> MessageRef | None:
        resp = await self._bot.edit_message(
            str(ref.channel_id),
            str(ref.message_id),
            message.text,
            wait=wait,
        )
        if resp is None or not resp.ok:
            return None
        if resp.ts is None:
            return None

        new_ref = MessageRef(
            channel_id=ref.channel_id,
            message_id=resp.ts,
            raw=resp.message,
            thread_id=ref.thread_id,
            sender_id=ref.sender_id,
        )

        followups = message.extra.get("followups") if message.extra else None
        if followups:
            await self._send_followups(
                followups,
                channel_id=str(ref.channel_id),
                thread_ts=str(ref.thread_id or ref.message_id),
            )

        return new_ref

    async def delete(self, *, ref: MessageRef) -> bool:
        # Don't delete messages by default in Tunapi
        return True

    async def close(self) -> None:
        await self._bot.close()

    async def _send_followups(
        self,
        followups: list[RenderedMessage],
        *,
        channel_id: str,
        thread_ts: str,
    ) -> None:
        for followup in followups:
            await self._bot.send_message(
                channel_id,
                followup.text,
                thread_ts=thread_ts,
            )


@dataclass(frozen=True, slots=True)
class SlackBridgeConfig:
    bot: SlackClient
    bot_user_id: str
    bot_username: str
    runtime: TransportRuntime
    channel_id: str | None
    startup_msg: str
    exec_cfg: ExecBridgeConfig
    session_mode: Literal["stateless", "chat"] = "stateless"
    show_resume_line: bool = True
    allowed_channel_ids: tuple[str, ...] = ()
    allowed_user_ids: tuple[str, ...] = ()
    message_overflow: str = "trim"
    trigger_mode: str = "mentions"
    files_enabled: bool = False
    files_uploads_dir: str = "incoming"
    files_deny_globs: tuple[str, ...] = (
        ".git/**",
        ".env",
        ".envrc",
        "*.pem",
        ".ssh/**",
    )
    files_max_upload_bytes: int = 20 * 1024 * 1024
    files_max_download_bytes: int = 50 * 1024 * 1024
    voice_enabled: bool = False
    voice_max_bytes: int = 10 * 1024 * 1024
    voice_model: str = "gpt-4o-mini-transcribe"
    voice_base_url: str | None = None
    voice_api_key: str | None = None
    projects_root: str | None = None
