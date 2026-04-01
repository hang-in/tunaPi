"""Dataclasses and helper functions for the Telegram event loop.

Extracted from ``loop.py`` to reduce its surface area.  These are
pure data containers and stateless helpers — no async I/O, no
transport calls.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..context import RunContext
from ..core.roundtable import RoundtableStore
from ..model import EngineId
from ..runners.run_options import EngineRunOptions
from ..transport import MessageRef
from .chat_prefs import ChatPrefsStore
from .chat_sessions import ChatSessionStore
from .commands.handlers import parse_slash_command
from .commands.parse import is_cancel_command
from .engine_overrides import merge_overrides
from .forward_coalescing import (
    ForwardKey,
    PendingPrompt,
    is_forwarded as _is_forwarded,
)
from .media_group_buffer import MediaGroupState
from .topic_state import TopicStateStore
from .types import TelegramIncomingMessage

if TYPE_CHECKING:
    from anyio.abc import TaskGroup

    from ..runner_bridge import RunningTasks
    from .bridge import TelegramBridgeConfig

MessageKey = tuple[int, int]
_SEEN_MESSAGES_LIMIT = 2048
_SEEN_UPDATES_LIMIT = 4096


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


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


@dataclass(frozen=True, slots=True)
class MessageClassification:
    text: str
    command_id: str | None
    args_text: str
    is_cancel: bool
    is_forward_candidate: bool
    is_media_group_document: bool


@dataclass(frozen=True, slots=True)
class TelegramCommandContext:
    cfg: TelegramBridgeConfig
    msg: TelegramIncomingMessage
    args_text: str
    ambient_context: RunContext | None
    topic_store: TopicStateStore | None
    chat_prefs: ChatPrefsStore | None
    resolved_scope: str | None
    scope_chat_ids: frozenset[int]
    reply: Callable[..., Awaitable[None]]
    task_group: TaskGroup


@dataclass(slots=True)
class TelegramLoopState:
    running_tasks: RunningTasks
    pending_prompts: dict[ForwardKey, PendingPrompt]
    media_groups: dict[tuple[int, str], MediaGroupState]
    command_ids: set[str]
    reserved_commands: set[str]
    reserved_chat_commands: set[str]
    transport_snapshot: dict[str, object] | None
    topic_store: TopicStateStore | None
    chat_session_store: ChatSessionStore | None
    chat_prefs: ChatPrefsStore | None
    resolved_topics_scope: str | None
    topics_chat_ids: frozenset[int]
    bot_username: str | None
    forward_coalesce_s: float
    media_group_debounce_s: float
    transport_id: str | None
    roundtable_store: RoundtableStore | None
    seen_update_ids: set[int]
    seen_update_order: deque[int]
    seen_message_keys: set[MessageKey]
    seen_messages_order: deque[MessageKey]


@dataclass(slots=True)
class TelegramLoopContext:
    """Runtime context bundling all shared state for dispatch functions.

    Allows closure functions to be extracted as regular functions
    that take this context as their first parameter.
    """

    cfg: TelegramBridgeConfig
    state: TelegramLoopState
    tg: TaskGroup
    scheduler: object  # ThreadScheduler (avoid circular import)
    forward_coalescer: object  # ForwardCoalescer
    media_group_buffer: object  # MediaGroupBuffer
    resume_resolver: object  # ResumeResolver


# ---------------------------------------------------------------------------
# Stateless helpers
# ---------------------------------------------------------------------------


def chat_session_key(
    msg: TelegramIncomingMessage, *, store: ChatSessionStore | None
) -> tuple[int, int | None] | None:
    if store is None or msg.thread_id is not None:
        return None
    if msg.chat_type == "private":
        return (msg.chat_id, None)
    if msg.sender_id is None:
        return None
    return (msg.chat_id, msg.sender_id)


async def resolve_engine_run_options(
    chat_id: int,
    thread_id: int | None,
    engine: EngineId,
    chat_prefs: ChatPrefsStore | None,
    topic_store: TopicStateStore | None,
) -> EngineRunOptions | None:
    topic_override = None
    if topic_store is not None and thread_id is not None:
        topic_override = await topic_store.get_engine_override(
            chat_id, thread_id, engine
        )
    chat_override = None
    if chat_prefs is not None:
        chat_override = await chat_prefs.get_engine_override(chat_id, engine)
    merged = merge_overrides(topic_override, chat_override)
    if merged is None:
        return None
    return EngineRunOptions(model=merged.model, reasoning=merged.reasoning)


def allowed_chat_ids(cfg: TelegramBridgeConfig) -> set[int]:
    allowed = set(cfg.chat_ids or ())
    allowed.add(cfg.chat_id)
    allowed.update(cfg.runtime.project_chat_ids())
    allowed.update(cfg.allowed_user_ids)
    return allowed


def classify_message(
    msg: TelegramIncomingMessage, *, files_enabled: bool
) -> MessageClassification:
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


def diff_keys(old: dict[str, object], new: dict[str, object]) -> list[str]:
    keys = set(old) | set(new)
    return sorted(key for key in keys if old.get(key) != new.get(key))
