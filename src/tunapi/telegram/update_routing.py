from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from collections.abc import Callable, Awaitable

from ..logging import get_logger

if TYPE_CHECKING:
    from anyio.abc import TaskGroup
    from .bridge import TelegramBridgeConfig
    from .types import TelegramIncomingUpdate, TelegramIncomingMessage
    from .loop import TelegramLoopState
    from ..scheduler import ThreadScheduler

logger = get_logger(__name__)

_SEEN_UPDATES_LIMIT = 4096
_SEEN_MESSAGES_LIMIT = 2048


@dataclass(frozen=True, slots=True)
class MessageClassification:
    text: str
    command_id: str | None
    args_text: str
    is_cancel: bool
    is_forward_candidate: bool
    is_media_group_document: bool


def _classify_message(
    msg: TelegramIncomingMessage, *, files_enabled: bool
) -> MessageClassification:
    from .commands.handlers import parse_slash_command
    from .commands.parse import is_cancel_command
    from .forward_coalescing import is_forwarded as _is_forwarded

    text = msg.text
    command_id, args_text = parse_slash_command(text)
    is_forward_candidate = (
        _is_forwarded(msg.raw)
        and msg.document is None
        and msg.voice is None
        and msg.media_group_id is None
    )
    is_media_group_document = (
        files_enabled and msg.document is not None and msg.media_group_id is not None
    )
    return MessageClassification(
        text=text,
        command_id=command_id,
        args_text=args_text,
        is_cancel=is_cancel_command(text),
        is_forward_candidate=is_forward_candidate,
        is_media_group_document=is_media_group_document,
    )


class TelegramUpdateRouter:
    def __init__(
        self,
        *,
        cfg: TelegramBridgeConfig,
        state: TelegramLoopState,
        tg: TaskGroup,
        scheduler: ThreadScheduler,
        route_message: Callable[[TelegramIncomingUpdate], Awaitable[None]],
    ) -> None:
        self._cfg = cfg
        self._state = state
        self._tg = tg
        self._scheduler = scheduler
        self._route_message = route_message
        self._allowed_user_ids = set(cfg.allowed_user_ids)

    async def route_update(self, update: TelegramIncomingUpdate) -> None:
        from .types import TelegramIncomingMessage, TelegramCallbackQuery
        from .bridge import CANCEL_CALLBACK_DATA
        from .commands.cancel import handle_callback_cancel

        if self._allowed_user_ids:
            sender_id = update.sender_id
            if sender_id is None or sender_id not in self._allowed_user_ids:
                logger.debug(
                    "update.ignored",
                    reason="sender_not_allowed",
                    chat_id=update.chat_id,
                    sender_id=sender_id,
                )
                return
        if update.update_id is not None:
            update_id = update.update_id
            if update_id in self._state.seen_update_ids:
                logger.debug(
                    "update.ignored",
                    reason="duplicate_update",
                    update_id=update_id,
                    chat_id=update.chat_id,
                    sender_id=update.sender_id,
                )
                return
            self._state.seen_update_ids.add(update_id)
            self._state.seen_update_order.append(update_id)
            if len(self._state.seen_update_order) > _SEEN_UPDATES_LIMIT:
                oldest_update_id = self._state.seen_update_order.popleft()
                self._state.seen_update_ids.discard(oldest_update_id)
        elif isinstance(update, TelegramIncomingMessage):
            key = (update.chat_id, update.message_id)
            if key in self._state.seen_message_keys:
                logger.debug(
                    "update.ignored",
                    reason="duplicate_message",
                    chat_id=update.chat_id,
                    message_id=update.message_id,
                    sender_id=update.sender_id,
                )
                return
            self._state.seen_message_keys.add(key)
            self._state.seen_messages_order.append(key)
            if len(self._state.seen_messages_order) > _SEEN_MESSAGES_LIMIT:
                oldest = self._state.seen_messages_order.popleft()
                self._state.seen_message_keys.discard(oldest)
        if isinstance(update, TelegramCallbackQuery):
            if update.data == CANCEL_CALLBACK_DATA:
                self._tg.start_soon(
                    handle_callback_cancel,
                    self._cfg,
                    update,
                    self._state.running_tasks,
                    self._scheduler,
                )
            else:
                self._tg.start_soon(
                    self._cfg.bot.answer_callback_query,
                    update.callback_query_id,
                )
            return
        await self._route_message(update)
