from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
import anyio

from tunapi.model import ResumeToken
from tunapi.markdown import MarkdownParts
from tunapi.transport import MessageRef, RenderedMessage, SendOptions
from .bridge import DiscordBridgeConfig
from .state import DiscordStateStore
from .render import prepare_discord
from .resume_queue import ResumeResolver as _ResumeResolverImpl

logger = get_logger = lambda name: logging.getLogger(name)


async def _send_startup(cfg: DiscordBridgeConfig, channel_id: int) -> None:
    """Send startup message to the specified channel."""
    logger("tunapi.discord.resume_dispatch").debug(
        "startup.message", text=cfg.startup_msg
    )
    parts = MarkdownParts(header=cfg.startup_msg)
    text = prepare_discord(parts)
    message = RenderedMessage(text=text, extra={})
    sent = await cfg.exec_cfg.transport.send(
        channel_id=channel_id,
        message=message,
    )
    if sent is not None:
        logger("tunapi.discord.resume_dispatch").info(
            "startup.sent", channel_id=channel_id
        )


async def _save_session_token(
    *,
    state_store: DiscordStateStore | None,
    guild_id: int | None,
    session_key: int,
    author_id: int | None,
    token: ResumeToken,
) -> None:
    if state_store is None or guild_id is None:
        return
    await state_store.set_session(
        guild_id, session_key, token.engine, token.value, author_id=author_id
    )


async def _wait_for_resume(running_task) -> ResumeToken | None:
    if running_task.resume is not None:
        return running_task.resume
    resume: ResumeToken | None = None

    async with anyio.create_task_group() as tg:

        async def wait_resume() -> None:
            nonlocal resume
            await running_task.resume_ready.wait()
            resume = running_task.resume
            tg.cancel_scope.cancel()

        async def wait_done() -> None:
            await running_task.done.wait()
            tg.cancel_scope.cancel()

        tg.start_soon(wait_resume)
        tg.start_soon(wait_done)

    return resume


async def _send_plain_reply(
    cfg: DiscordBridgeConfig,
    *,
    channel_id: int,
    user_msg_id: int,
    thread_id: int | None,
    text: str,
) -> None:
    parts = MarkdownParts(header=text)
    rendered_text = prepare_discord(parts)
    reply_ref = MessageRef(
        channel_id=channel_id,
        message_id=user_msg_id,
        thread_id=thread_id,
    )
    await cfg.exec_cfg.transport.send(
        channel_id=channel_id,
        message=RenderedMessage(text=rendered_text, extra={"show_cancel": False}),
        options=SendOptions(reply_to=reply_ref, notify=False, thread_id=thread_id),
    )


async def _send_queued_progress(
    cfg: DiscordBridgeConfig,
    *,
    channel_id: int,
    user_msg_id: int,
    thread_id: int | None,
    resume_token: ResumeToken,
    context: Any,
) -> MessageRef | None:
    from .resume_queue import _send_queued_progress as _send_queued_progress_impl

    return await _send_queued_progress_impl(
        cfg,
        channel_id=channel_id,
        user_msg_id=user_msg_id,
        thread_id=thread_id,
        resume_token=resume_token,
        context=context,
    )


async def send_with_resume(
    cfg: DiscordBridgeConfig,
    enqueue: Callable[
        [
            int,
            int,
            str,
            ResumeToken,
            Any,
            int | None,
            tuple[int, int | None] | None,
            MessageRef | None,
        ],
        Awaitable[None],
    ],
    running_task,
    channel_id: int,
    user_msg_id: int,
    thread_id: int | None,
    session_key: tuple[int, int | None] | None,
    text: str,
) -> None:
    from .resume_queue import send_with_resume as _send_with_resume_impl
    import tunapi.discord.loop as loop

    await _send_with_resume_impl(
        cfg,
        enqueue,
        running_task,
        channel_id=channel_id,
        user_msg_id=user_msg_id,
        thread_id=thread_id,
        session_key=session_key,
        text=text,
        wait_for_resume=loop._wait_for_resume,
        send_plain_reply=loop._send_plain_reply,
        send_queued_progress=loop._send_queued_progress,
    )


class ResumeResolver(_ResumeResolverImpl):
    def __init__(
        self,
        *,
        cfg: DiscordBridgeConfig,
        task_group,
        running_tasks: Mapping[MessageRef, object],
        enqueue_resume: Callable[
            [
                int,
                int,
                str,
                ResumeToken,
                Any,
                int | None,
                tuple[int, int | None] | None,
                MessageRef | None,
            ],
            Awaitable[None],
        ],
    ) -> None:
        import tunapi.discord.loop as loop

        super().__init__(
            cfg=cfg,
            task_group=task_group,
            running_tasks=running_tasks,
            enqueue_resume=enqueue_resume,
            send_with_resume_fn=loop.send_with_resume,
        )
