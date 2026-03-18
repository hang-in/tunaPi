"""Mattermost Transport and Presenter implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..logging import get_logger
from ..markdown import MarkdownFormatter, assemble_markdown_parts
from ..progress import ProgressState
from ..runner_bridge import ExecBridgeConfig
from ..transport import MessageRef, RenderedMessage, SendOptions
from ..transport_runtime import TransportRuntime
from .client import MattermostClient
from .render import prepare_mattermost, prepare_mattermost_multi

logger = get_logger(__name__)

# Mattermost cancel via 🛑 reaction — no inline buttons needed
CANCEL_EMOJI = "octagonal_sign"


class MattermostPresenter:
    """Renders :class:`ProgressState` into Mattermost Markdown messages."""

    def __init__(
        self,
        *,
        formatter: MarkdownFormatter | None = None,
        message_overflow: str = "trim",
        show_resume_line: bool = True,
    ) -> None:
        self._formatter = formatter or MarkdownFormatter()
        self._message_overflow = message_overflow
        self._show_resume_line = show_resume_line

    def _strip_resume(self, state: ProgressState) -> ProgressState:
        """Return a copy of state with resume_line cleared when hidden."""
        if self._show_resume_line or not state.resume_line:
            return state
        return ProgressState(
            engine=state.engine,
            action_count=state.action_count,
            actions=state.actions,
            resume=state.resume,
            resume_line=None,
            context_line=state.context_line,
        )

    def render_progress(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        label: str = "working",
    ) -> RenderedMessage:
        state = self._strip_resume(state)
        parts = self._formatter.render_progress_parts(
            state, elapsed_s=elapsed_s, label=label
        )
        text = prepare_mattermost(parts)
        return RenderedMessage(text=text)

    def render_final(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        status: str,
        answer: str,
    ) -> RenderedMessage:
        state = self._strip_resume(state)
        parts = self._formatter.render_final_parts(
            state, elapsed_s=elapsed_s, status=status, answer=answer
        )

        if self._message_overflow == "split":
            messages = prepare_mattermost_multi(parts)
            if len(messages) > 1:
                followups = [
                    RenderedMessage(text=msg) for msg in messages[1:]
                ]
                return RenderedMessage(
                    text=messages[0],
                    extra={"followups": followups},
                )
            return RenderedMessage(text=messages[0])

        text = prepare_mattermost(parts)
        return RenderedMessage(text=text)


class MattermostTransport:
    """Implements the :class:`Transport` protocol for Mattermost."""

    def __init__(self, bot: MattermostClient) -> None:
        self._bot = bot

    async def send(
        self,
        *,
        channel_id: str | int,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef | None:
        ch = str(channel_id)
        root_id: str | None = None
        if options is not None:
            # Only thread when explicitly requested (user replied in a thread)
            if options.thread_id is not None:
                root_id = str(options.thread_id)
            # Don't delete the progress message — leave it in channel

        props = message.extra.get("props") if message.extra else None
        file_ids = message.extra.get("file_ids") if message.extra else None

        post = await self._bot.send_message(
            ch,
            message.text,
            root_id=root_id,
            props=props,
            file_ids=file_ids,
        )
        if post is None:
            return None

        ref = MessageRef(
            channel_id=ch,
            message_id=post.id,
            raw=post,
            thread_id=post.root_id or post.id,
        )

        # Send followup messages (for split overflow)
        followups = message.extra.get("followups") if message.extra else None
        if followups:
            await self._send_followups(
                followups,
                channel_id=ch,
                root_id=post.root_id or post.id,
            )

        return ref

    async def edit(
        self,
        *,
        ref: MessageRef,
        message: RenderedMessage,
        wait: bool = True,
    ) -> MessageRef | None:
        props = message.extra.get("props") if message.extra else None
        post = await self._bot.edit_message(
            str(ref.message_id),
            message.text,
            props=props,
            wait=wait,
        )
        if post is None:
            return None

        new_ref = MessageRef(
            channel_id=ref.channel_id,
            message_id=post.id,
            raw=post,
            thread_id=ref.thread_id,
        )

        followups = message.extra.get("followups") if message.extra else None
        if followups:
            await self._send_followups(
                followups,
                channel_id=str(ref.channel_id),
                root_id=str(ref.thread_id or ref.message_id),
            )

        return new_ref

    async def delete(self, *, ref: MessageRef) -> bool:
        # Don't delete messages — leave progress messages visible in channel
        return True

    async def close(self) -> None:
        await self._bot.close()

    async def _send_followups(
        self,
        followups: list[RenderedMessage],
        *,
        channel_id: str,
        root_id: str,
    ) -> None:
        for followup in followups:
            await self._bot.send_message(
                channel_id,
                followup.text,
                root_id=root_id,
            )


@dataclass(frozen=True, slots=True)
class MattermostBridgeConfig:
    bot: MattermostClient
    bot_user_id: str
    runtime: TransportRuntime
    channel_id: str
    startup_msg: str
    exec_cfg: ExecBridgeConfig
    session_mode: Literal["stateless", "chat"] = "stateless"
    show_resume_line: bool = True
    allowed_channel_ids: tuple[str, ...] = ()
    allowed_user_ids: tuple[str, ...] = ()
    message_overflow: str = "trim"
