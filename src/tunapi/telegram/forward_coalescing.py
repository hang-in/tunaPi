"""Forward message coalescing for Telegram transport.

When users forward multiple messages before typing a prompt, this module
debounces the incoming forwards and attaches them to the final prompt.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio
from anyio.abc import TaskGroup

from ..logging import get_logger

if TYPE_CHECKING:
    from .types import TelegramIncomingMessage

logger = get_logger(__name__)

ForwardKey = tuple[int, int, int]

_FORWARD_FIELDS = (
    "forward_origin",
    "forward_from",
    "forward_from_chat",
    "forward_from_message_id",
    "forward_sender_name",
    "forward_signature",
    "forward_date",
    "is_automatic_forward",
)


def forward_key(msg: TelegramIncomingMessage) -> ForwardKey:
    return (msg.chat_id, msg.thread_id or 0, msg.sender_id or 0)


def is_forwarded(raw: dict[str, object] | None) -> bool:
    if not isinstance(raw, dict):
        return False
    return any(raw.get(field) is not None for field in _FORWARD_FIELDS)


def forward_fields_present(raw: dict[str, object] | None) -> list[str]:
    if not isinstance(raw, dict):
        return []
    return [field for field in _FORWARD_FIELDS if raw.get(field) is not None]


def format_forwarded_prompt(forwarded: list[str], prompt: str) -> str:
    if not forwarded:
        return prompt
    separator = "\n\n"
    forward_block = separator.join(forwarded)
    if prompt.strip():
        return f"{prompt}{separator}{forward_block}"
    return forward_block


@dataclass(slots=True)
class PendingPrompt:
    msg: TelegramIncomingMessage
    text: str
    ambient_context: object  # RunContext | None
    chat_project: str | None
    topic_key: tuple[int, int] | None
    chat_session_key: tuple[int, int | None] | None
    reply_ref: object  # MessageRef | None
    reply_id: int | None
    is_voice_transcribed: bool
    forwards: list[tuple[int, str]]
    cancel_scope: anyio.CancelScope | None = None


class ForwardCoalescer:
    def __init__(
        self,
        *,
        task_group: TaskGroup,
        debounce_s: float,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
        dispatch: Callable[[PendingPrompt], Awaitable[None]],
        pending: dict[ForwardKey, PendingPrompt],
    ) -> None:
        self._task_group = task_group
        self._debounce_s = debounce_s
        self._sleep = sleep
        self._dispatch = dispatch
        self._pending = pending

    def cancel(self, key: ForwardKey) -> None:
        pending = self._pending.pop(key, None)
        if pending is None:
            return
        if pending.cancel_scope is not None:
            pending.cancel_scope.cancel()
        logger.debug(
            "forward.prompt.cancelled",
            chat_id=pending.msg.chat_id,
            thread_id=pending.msg.thread_id,
            sender_id=pending.msg.sender_id,
            message_id=pending.msg.message_id,
            forward_count=len(pending.forwards),
        )

    def schedule(self, pending: PendingPrompt) -> None:
        if pending.msg.sender_id is None:
            logger.debug(
                "forward.prompt.bypass",
                chat_id=pending.msg.chat_id,
                thread_id=pending.msg.thread_id,
                sender_id=pending.msg.sender_id,
                message_id=pending.msg.message_id,
                reason="missing_sender",
            )
            self._task_group.start_soon(self._dispatch, pending)
            return
        if self._debounce_s <= 0:
            logger.debug(
                "forward.prompt.bypass",
                chat_id=pending.msg.chat_id,
                thread_id=pending.msg.thread_id,
                sender_id=pending.msg.sender_id,
                message_id=pending.msg.message_id,
                reason="disabled",
            )
            self._task_group.start_soon(self._dispatch, pending)
            return
        key = forward_key(pending.msg)
        existing = self._pending.get(key)
        if existing is not None:
            if existing.cancel_scope is not None:
                existing.cancel_scope.cancel()
            if existing.forwards:
                pending.forwards = list(existing.forwards)
            logger.debug(
                "forward.prompt.replace",
                chat_id=pending.msg.chat_id,
                thread_id=pending.msg.thread_id,
                sender_id=pending.msg.sender_id,
                old_message_id=existing.msg.message_id,
                new_message_id=pending.msg.message_id,
                forward_count=len(pending.forwards),
            )
        self._pending[key] = pending
        logger.debug(
            "forward.prompt.schedule",
            chat_id=pending.msg.chat_id,
            thread_id=pending.msg.thread_id,
            sender_id=pending.msg.sender_id,
            message_id=pending.msg.message_id,
            debounce_s=self._debounce_s,
        )
        self._reschedule(key, pending)

    def attach_forward(self, msg: TelegramIncomingMessage) -> None:
        if msg.sender_id is None:
            logger.debug(
                "forward.message.ignored",
                chat_id=msg.chat_id,
                thread_id=msg.thread_id,
                sender_id=msg.sender_id,
                message_id=msg.message_id,
                reason="missing_sender",
            )
            return
        key = forward_key(msg)
        pending = self._pending.get(key)
        if pending is None:
            logger.debug(
                "forward.message.ignored",
                chat_id=msg.chat_id,
                thread_id=msg.thread_id,
                sender_id=msg.sender_id,
                message_id=msg.message_id,
                reason="no_pending_prompt",
            )
            return
        text = msg.text
        if not text.strip():
            logger.debug(
                "forward.message.ignored",
                chat_id=msg.chat_id,
                thread_id=msg.thread_id,
                sender_id=msg.sender_id,
                message_id=msg.message_id,
                reason="empty_text",
            )
            return
        pending.forwards.append((msg.message_id, text))
        logger.debug(
            "forward.message.attached",
            chat_id=msg.chat_id,
            thread_id=msg.thread_id,
            sender_id=msg.sender_id,
            message_id=msg.message_id,
            prompt_message_id=pending.msg.message_id,
            forward_count=len(pending.forwards),
            forward_fields=forward_fields_present(msg.raw),
            forward_date=msg.raw.get("forward_date") if msg.raw else None,
            message_date=msg.raw.get("date") if msg.raw else None,
            text_len=len(text),
        )
        self._reschedule(key, pending)

    def _reschedule(self, key: ForwardKey, pending: PendingPrompt) -> None:
        if pending.cancel_scope is not None:
            pending.cancel_scope.cancel()
        pending.cancel_scope = None
        self._task_group.start_soon(self._debounce_prompt_run, key, pending)

    async def _debounce_prompt_run(
        self,
        key: ForwardKey,
        pending: PendingPrompt,
    ) -> None:
        try:
            with anyio.CancelScope() as scope:
                pending.cancel_scope = scope
                await self._sleep(self._debounce_s)
        except anyio.get_cancelled_exc_class():
            return
        if self._pending.get(key) is not pending:
            return
        self._pending.pop(key, None)
        logger.debug(
            "forward.prompt.run",
            chat_id=pending.msg.chat_id,
            thread_id=pending.msg.thread_id,
            sender_id=pending.msg.sender_id,
            message_id=pending.msg.message_id,
            forward_count=len(pending.forwards),
            debounce_s=self._debounce_s,
        )
        await self._dispatch(pending)
