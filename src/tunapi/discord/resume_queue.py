from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from tunapi.model import ResumeToken
from tunapi.progress import ProgressTracker
from tunapi.transport import MessageRef, RenderedMessage, SendOptions

from .loop_state import ResumeDecision

if TYPE_CHECKING:
    from tunapi.context import RunContext

    from .bridge import DiscordBridgeConfig


async def _send_queued_progress(
    cfg: DiscordBridgeConfig,
    *,
    channel_id: int,
    user_msg_id: int,
    thread_id: int | None,
    resume_token: ResumeToken,
    context: RunContext | None,
) -> MessageRef | None:
    tracker = ProgressTracker(engine=resume_token.engine)
    tracker.set_resume(resume_token)
    context_line = cfg.runtime.format_context_line(context)
    state = tracker.snapshot(context_line=context_line)
    queued = cfg.exec_cfg.presenter.render_progress(
        state,
        elapsed_s=0.0,
        label="queued",
    )
    message = RenderedMessage(
        text=queued.text,
        extra={**queued.extra, "show_cancel": False},
    )
    reply_ref = MessageRef(
        channel_id=channel_id,
        message_id=user_msg_id,
        thread_id=thread_id,
    )
    return await cfg.exec_cfg.transport.send(
        channel_id=channel_id,
        message=message,
        options=SendOptions(reply_to=reply_ref, notify=False, thread_id=thread_id),
    )


async def send_with_resume(
    cfg: DiscordBridgeConfig,
    enqueue: Callable[
        [
            int,
            int,
            str,
            ResumeToken,
            RunContext | None,
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
    *,
    wait_for_resume,
    send_plain_reply,
    send_queued_progress=_send_queued_progress,
) -> None:
    resume = await wait_for_resume(running_task)
    if resume is None:
        await send_plain_reply(
            cfg,
            channel_id=channel_id,
            user_msg_id=user_msg_id,
            thread_id=thread_id,
            text="resume token not ready yet; try replying to the final message.",
        )
        return
    progress_ref = await send_queued_progress(
        cfg,
        channel_id=channel_id,
        user_msg_id=user_msg_id,
        thread_id=thread_id,
        resume_token=resume,
        context=running_task.context,
    )
    await enqueue(
        channel_id,
        user_msg_id,
        text,
        resume,
        running_task.context,
        thread_id,
        session_key,
        progress_ref,
    )


class ResumeResolver:
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
                RunContext | None,
                int | None,
                tuple[int, int | None] | None,
                MessageRef | None,
            ],
            Awaitable[None],
        ],
        send_with_resume_fn,
    ) -> None:
        self._cfg = cfg
        self._task_group = task_group
        self._running_tasks = running_tasks
        self._enqueue_resume = enqueue_resume
        self._send_with_resume = send_with_resume_fn

    async def resolve(
        self,
        *,
        resume_token: ResumeToken | None,
        reply_id: int | None,
        chat_id: int,
        user_msg_id: int,
        thread_id: int | None,
        session_key: tuple[int, int | None] | None,
        prompt_text: str,
    ) -> ResumeDecision:
        if resume_token is not None:
            return ResumeDecision(
                resume_token=resume_token,
                handled_by_running_task=False,
            )
        if reply_id is not None:
            running_task = self._running_tasks.get(
                MessageRef(channel_id=chat_id, message_id=reply_id)
            )
            if running_task is not None:
                self._task_group.start_soon(
                    self._send_with_resume,
                    self._cfg,
                    self._enqueue_resume,
                    running_task,
                    chat_id,
                    user_msg_id,
                    thread_id,
                    session_key,
                    prompt_text,
                )
                return ResumeDecision(resume_token=None, handled_by_running_task=True)
        return ResumeDecision(resume_token=None, handled_by_running_task=False)
