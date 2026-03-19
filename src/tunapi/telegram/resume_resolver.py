"""Resume token resolution for Telegram transport.

Determines whether an incoming message should use an existing resume token
(from topic store, chat session store, or running task), or start fresh.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from anyio.abc import TaskGroup

from ..model import EngineId, ResumeToken
from ..transport import MessageRef

if TYPE_CHECKING:
    from ..context import RunContext
    from .bridge import TelegramBridgeConfig
    from .chat_sessions import ChatSessionStore
    from .topics import TopicStateStore


@dataclass(frozen=True, slots=True)
class ResumeDecision:
    resume_token: ResumeToken | None
    handled_by_running_task: bool


class ResumeResolver:
    def __init__(
        self,
        *,
        cfg: TelegramBridgeConfig,
        task_group: TaskGroup,
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
        topic_store: TopicStateStore | None,
        chat_session_store: ChatSessionStore | None,
    ) -> None:
        self._cfg = cfg
        self._task_group = task_group
        self._running_tasks = running_tasks
        self._enqueue_resume = enqueue_resume
        self._topic_store = topic_store
        self._chat_session_store = chat_session_store

    async def resolve(
        self,
        *,
        resume_token: ResumeToken | None,
        reply_id: int | None,
        chat_id: int,
        user_msg_id: int,
        thread_id: int | None,
        chat_session_key: tuple[int, int | None] | None,
        topic_key: tuple[int, int] | None,
        engine_for_session: EngineId,
        prompt_text: str,
    ) -> ResumeDecision:
        if resume_token is not None:
            return ResumeDecision(
                resume_token=resume_token, handled_by_running_task=False
            )
        if reply_id is not None:
            running_task = self._running_tasks.get(
                MessageRef(channel_id=chat_id, message_id=reply_id)
            )
            if running_task is not None:
                # Lazy import to avoid circular dependency
                from .loop import send_with_resume

                self._task_group.start_soon(
                    send_with_resume,
                    self._cfg,
                    self._enqueue_resume,
                    running_task,
                    chat_id,
                    user_msg_id,
                    thread_id,
                    chat_session_key,
                    prompt_text,
                )
                return ResumeDecision(resume_token=None, handled_by_running_task=True)
        if self._topic_store is not None and topic_key is not None:
            stored = await self._topic_store.get_session_resume(
                topic_key[0],
                topic_key[1],
                engine_for_session,
            )
            if stored is not None:
                resume_token = stored
        if (
            resume_token is None
            and self._chat_session_store is not None
            and chat_session_key is not None
        ):
            stored = await self._chat_session_store.get_session_resume(
                chat_session_key[0],
                chat_session_key[1],
                engine_for_session,
            )
            if stored is not None:
                resume_token = stored
        return ResumeDecision(resume_token=resume_token, handled_by_running_task=False)
