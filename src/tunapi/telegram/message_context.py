from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import TelegramBridgeConfig
    from ..core.chat_sessions import ChatSessionStore
    from .topic_state import TopicStateStore
    from ..core.chat_prefs import ChatPrefsStore
    from .types import TelegramIncomingMessage
    from ..transport import MessageRef
    from ..context import RunContext


@dataclass(frozen=True, slots=True)
class TelegramMsgContext:
    chat_id: int
    thread_id: int | None
    reply_id: int | None
    reply_ref: MessageRef | None
    topic_key: tuple[int, int] | None
    chat_session_key: tuple[int, int | None] | None
    stateful_mode: bool
    chat_project: str | None
    ambient_context: RunContext | None


class TelegramContextBuilder:
    def __init__(
        self,
        *,
        cfg: TelegramBridgeConfig,
        chat_session_store: ChatSessionStore | None,
        topic_store: TopicStateStore | None,
        chat_prefs: ChatPrefsStore | None,
        topics_chat_ids: frozenset[int],
    ) -> None:
        self._cfg = cfg
        self._chat_session_store = chat_session_store
        self._topic_store = topic_store
        self._chat_prefs = chat_prefs
        self._topics_chat_ids = topics_chat_ids

    def resolve_topic_key(
        self,
        msg: TelegramIncomingMessage,
    ) -> tuple[int, int] | None:
        from .topics import _topic_key

        if self._topic_store is None:
            return None
        return _topic_key(msg, self._cfg, scope_chat_ids=self._topics_chat_ids)

    async def build(
        self,
        msg: TelegramIncomingMessage,
    ) -> TelegramMsgContext:
        from .loop_state import chat_session_key
        from .topics import _topics_chat_project
        from .context import _merge_topic_context
        from ..transport import MessageRef

        chat_id = msg.chat_id
        reply_id = msg.reply_to_message_id
        reply_ref = (
            MessageRef(channel_id=chat_id, message_id=reply_id)
            if reply_id is not None
            else None
        )
        topic_key = self.resolve_topic_key(msg)
        session_key = chat_session_key(msg, store=self._chat_session_store)

        stateful_mode = topic_key is not None or session_key is not None
        chat_project = (
            _topics_chat_project(self._cfg, chat_id)
            if self._cfg.topics.enabled
            else None
        )
        bound_context = (
            await self._topic_store.get_context(*topic_key)
            if self._topic_store is not None and topic_key is not None
            else None
        )
        chat_bound_context = None
        if self._chat_prefs is not None:
            chat_bound_context = await self._chat_prefs.get_context(str(chat_id))

        if bound_context is not None:
            ambient_context = _merge_topic_context(
                chat_project=chat_project, bound=bound_context
            )
        elif chat_bound_context is not None:
            ambient_context = chat_bound_context
        else:
            ambient_context = _merge_topic_context(
                chat_project=chat_project, bound=None
            )
        return TelegramMsgContext(
            chat_id=chat_id,
            thread_id=msg.thread_id,
            reply_id=reply_id,
            reply_ref=reply_ref,
            topic_key=topic_key,
            chat_session_key=session_key,
            stateful_mode=stateful_mode,
            chat_project=chat_project,
            ambient_context=ambient_context,
        )
