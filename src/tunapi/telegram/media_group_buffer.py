"""Media group buffering for Telegram transport.

Telegram sends each file in a media group as a separate update.  This module
debounces them into a single batch so they can be handled together.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from anyio.abc import TaskGroup

from ..logging import get_logger

if TYPE_CHECKING:
    from .bridge import TelegramBridgeConfig
    from .chat_prefs import ChatPrefsStore
    from .topics import TopicStateStore
    from .types import TelegramIncomingMessage

logger = get_logger(__name__)


@dataclass(slots=True)
class MediaGroupState:
    messages: list[TelegramIncomingMessage]
    token: int = 0


class MediaGroupBuffer:
    def __init__(
        self,
        *,
        task_group: TaskGroup,
        debounce_s: float,
        sleep: Callable[[float], Awaitable[None]],
        cfg: TelegramBridgeConfig,
        chat_prefs: ChatPrefsStore | None,
        topic_store: TopicStateStore | None,
        bot_username: str | None,
        command_ids: Callable[[], set[str]],
        reserved_chat_commands: set[str],
        groups: dict[tuple[int, str], MediaGroupState],
        run_prompt_from_upload: Callable[..., Awaitable[None]],
        resolve_prompt_message: Callable[..., Awaitable[object | None]],
    ) -> None:
        self._task_group = task_group
        self._debounce_s = debounce_s
        self._sleep = sleep
        self._cfg = cfg
        self._chat_prefs = chat_prefs
        self._topic_store = topic_store
        self._bot_username = bot_username
        self._command_ids = command_ids
        self._reserved_chat_commands = reserved_chat_commands
        self._groups = groups
        self._run_prompt_from_upload = run_prompt_from_upload
        self._resolve_prompt_message = resolve_prompt_message

    def add(self, msg: TelegramIncomingMessage) -> None:
        if msg.media_group_id is None:
            return
        key = (msg.chat_id, msg.media_group_id)
        state = self._groups.get(key)
        if state is None:
            state = MediaGroupState(messages=[])
            self._groups[key] = state
            self._task_group.start_soon(self._flush_media_group, key)
        state.messages.append(msg)
        state.token += 1

    async def _flush_media_group(self, key: tuple[int, str]) -> None:
        from .commands.handlers import handle_media_group
        from .trigger_mode import resolve_trigger_mode, should_trigger_run

        while True:
            state = self._groups.get(key)
            if state is None:
                return
            token = state.token
            await self._sleep(self._debounce_s)
            state = self._groups.get(key)
            if state is None:
                return
            if state.token != token:
                continue
            messages = list(state.messages)
            del self._groups[key]
            if not messages:
                return
            trigger_mode = await resolve_trigger_mode(
                chat_id=messages[0].chat_id,
                thread_id=messages[0].thread_id,
                chat_prefs=self._chat_prefs,
                topic_store=self._topic_store,
            )
            command_ids = self._command_ids()
            if trigger_mode == "mentions" and not any(
                should_trigger_run(
                    msg,
                    bot_username=self._bot_username,
                    runtime=self._cfg.runtime,
                    command_ids=command_ids,
                    reserved_chat_commands=self._reserved_chat_commands,
                )
                for msg in messages
            ):
                return
            await handle_media_group(
                self._cfg,
                messages,
                self._topic_store,
                self._run_prompt_from_upload,
                self._resolve_prompt_message,
            )
            return
