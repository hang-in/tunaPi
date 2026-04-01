"""Builtin command router for the Telegram event loop.

Extracted from ``loop.py``.  Routes ``/file``, ``/ctx``, ``/new``,
``/topic``, ``/model``, ``/agent``, ``/reasoning``, ``/trigger``,
``/rt`` to their respective handler coroutines.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from ..core.roundtable import (
    RoundtableSession,
    RoundtableStore,
    handle_rt,
    run_followup_round,
    run_roundtable,
)
from ..logging import get_logger
from ..transport import RenderedMessage, SendOptions
from .commands.handlers import (
    handle_agent_command,
    handle_chat_ctx_command,
    handle_ctx_command,
    handle_file_command,
    handle_model_command,
    handle_new_command,
    handle_reasoning_command,
    handle_topic_command,
    handle_trigger_command,
)
from .topics import _topic_key

if TYPE_CHECKING:
    from ..runner_bridge import RunningTasks
    from .bridge import TelegramBridgeConfig
    from .chat_prefs import ChatPrefsStore
    from .loop_state import TelegramCommandContext, TelegramLoopContext
    from .types import TelegramIncomingMessage

_logger = get_logger(__name__)

# Callback type for sending a message to the current chat.
type _SendFn = Callable[[RenderedMessage], Awaitable[None]]


def dispatch_builtin_command(
    *,
    ctx: TelegramCommandContext,
    command_id: str,
) -> bool:
    """Route a builtin command.  Returns True if dispatched."""
    cfg = ctx.cfg
    msg = ctx.msg
    args_text = ctx.args_text
    ambient_context = ctx.ambient_context
    topic_store = ctx.topic_store
    chat_prefs = ctx.chat_prefs
    resolved_scope = ctx.resolved_scope
    scope_chat_ids = ctx.scope_chat_ids
    reply = ctx.reply
    task_group = ctx.task_group

    if command_id == "file":
        if not cfg.files.enabled:
            handler = partial(
                reply,
                text="file transfer disabled; enable `[transports.telegram.files]`.",
            )
        else:
            handler = partial(
                handle_file_command,
                cfg,
                msg,
                args_text,
                ambient_context,
                topic_store,
            )
        task_group.start_soon(handler)
        return True

    if command_id == "ctx":
        topic_key = (
            _topic_key(msg, cfg, scope_chat_ids=scope_chat_ids)
            if cfg.topics.enabled and topic_store is not None
            else None
        )
        if topic_key is not None:
            handler = partial(
                handle_ctx_command,
                cfg,
                msg,
                args_text,
                topic_store,
                resolved_scope=resolved_scope,
                scope_chat_ids=scope_chat_ids,
            )
        else:
            handler = partial(
                handle_chat_ctx_command,
                cfg,
                msg,
                args_text,
                chat_prefs,
            )
        task_group.start_soon(handler)
        return True

    if cfg.topics.enabled and topic_store is not None:
        if command_id == "new":
            handler = partial(
                handle_new_command,
                cfg,
                msg,
                topic_store,
                resolved_scope=resolved_scope,
                scope_chat_ids=scope_chat_ids,
            )
        elif command_id == "topic":
            handler = partial(
                handle_topic_command,
                cfg,
                msg,
                args_text,
                topic_store,
                resolved_scope=resolved_scope,
                scope_chat_ids=scope_chat_ids,
            )
        else:
            handler = None
        if handler is not None:
            task_group.start_soon(handler)
            return True

    if command_id == "model":
        handler = partial(
            handle_model_command,
            cfg,
            msg,
            args_text,
            ambient_context,
            topic_store,
            chat_prefs,
            resolved_scope=resolved_scope,
            scope_chat_ids=scope_chat_ids,
        )
        task_group.start_soon(handler)
        return True

    if command_id == "agent":
        handler = partial(
            handle_agent_command,
            cfg,
            msg,
            args_text,
            ambient_context,
            topic_store,
            chat_prefs,
            resolved_scope=resolved_scope,
            scope_chat_ids=scope_chat_ids,
        )
        task_group.start_soon(handler)
        return True

    if command_id == "reasoning":
        handler = partial(
            handle_reasoning_command,
            cfg,
            msg,
            args_text,
            ambient_context,
            topic_store,
            chat_prefs,
            resolved_scope=resolved_scope,
            scope_chat_ids=scope_chat_ids,
        )
        task_group.start_soon(handler)
        return True

    if command_id == "trigger":
        handler = partial(
            handle_trigger_command,
            cfg,
            msg,
            args_text,
            ambient_context,
            topic_store,
            chat_prefs,
            resolved_scope=resolved_scope,
            scope_chat_ids=scope_chat_ids,
        )
        task_group.start_soon(handler)
        return True

    return False


# ---------------------------------------------------------------------------
# Roundtable helpers (full-loop dispatch)
# ---------------------------------------------------------------------------


async def _start_roundtable(
    chat_id: int,
    topic: str,
    rounds: int,
    engines: list[str],
    *,
    cfg: TelegramBridgeConfig,
    running_tasks: RunningTasks,
    chat_prefs: ChatPrefsStore | None,
    roundtables: RoundtableStore,
) -> None:
    """Create a roundtable header message and run all rounds."""
    engines_display = ", ".join(f"`{e}`" for e in engines)
    rounds_display = f"{rounds} round{'s' if rounds > 1 else ''}"
    header = (
        f"*Roundtable*\n\n"
        f"*Topic:* {topic}\n"
        f"*Engines:* {engines_display} | *Rounds:* {rounds_display}\n\n"
        f"---"
    )
    ref = await cfg.exec_cfg.transport.send(
        channel_id=chat_id,
        message=RenderedMessage(text=header),
    )
    if ref is None:
        _logger.error("roundtable.header_send_failed", chat_id=chat_id)
        return

    thread_id = str(ref.message_id)
    session = RoundtableSession(
        thread_id=thread_id,
        channel_id=chat_id,
        topic=topic,
        engines=engines,
        total_rounds=rounds,
    )
    roundtables.put(session)

    # Resolve ambient context (channel-bound project)
    ambient_context = None
    if chat_prefs:
        ambient_context = await chat_prefs.get_context(chat_id)

    _logger.info(
        "roundtable.start",
        thread_id=thread_id,
        topic=topic,
        engines=engines,
        rounds=rounds,
    )

    try:
        await run_roundtable(
            session,
            cfg=cfg,
            chat_prefs=chat_prefs,
            running_tasks=running_tasks,
            ambient_context=ambient_context,
        )
    finally:
        roundtables.complete(thread_id)


async def _archive_roundtable(
    session: RoundtableSession,
    send: _SendFn,
) -> None:
    """Notify that the roundtable is closed."""
    await send(RenderedMessage(text="Roundtable closed."))


async def dispatch_rt_command(
    ctx: TelegramLoopContext,
    msg: TelegramIncomingMessage,
    args: str,
) -> None:
    """Handle ``!rt`` / ``/rt`` in the Telegram transport."""
    cfg = ctx.cfg
    state = ctx.state
    roundtables = state.roundtable_store
    if roundtables is None:
        return
    running_tasks = state.running_tasks
    chat_prefs = state.chat_prefs
    chat_id = msg.chat_id
    thread_id = str(msg.thread_id) if msg.thread_id is not None else None

    continue_rt: Callable[[str, list[str] | None], Awaitable[None]] | None = None
    close_rt: Callable[[], Awaitable[None]] | None = None

    async def _send(message: RenderedMessage) -> None:
        await cfg.exec_cfg.transport.send(
            channel_id=chat_id,
            message=message,
            options=SendOptions(thread_id=msg.thread_id),
        )

    if thread_id and roundtables:
        completed_session = roundtables.get_completed(thread_id)
        if completed_session:
            ambient_ctx = (
                await chat_prefs.get_context(chat_id) if chat_prefs else None
            )

            async def continue_rt(
                topic: str,
                engines_filter: list[str] | None,
                *,
                _s: Any = completed_session,
                _ctx: Any = ambient_ctx,
            ) -> None:
                await run_followup_round(
                    _s,
                    topic,
                    engines_filter,
                    cfg=cfg,
                    running_tasks=running_tasks,
                    ambient_context=_ctx,
                )

            async def close_rt(
                *,
                _tid: str = thread_id,
                _rt: RoundtableStore = roundtables,
                _s: RoundtableSession = completed_session,
            ) -> None:
                await _archive_roundtable(_s, _send)
                _rt.remove(_tid)

        # Allow close on active (non-completed) sessions too
        active_session = roundtables.get(thread_id)
        if active_session and not active_session.completed and close_rt is None:

            async def close_rt(
                *,
                _tid: str = thread_id,
                _rt: RoundtableStore = roundtables,
            ) -> None:
                session = _rt.get(_tid)
                if session:
                    session.cancel_event.set()
                    await _archive_roundtable(session, _send)
                _rt.remove(_tid)

    await handle_rt(
        args,
        runtime=cfg.runtime,
        send=_send,
        start_roundtable=lambda topic, rounds, engines: _start_roundtable(
            chat_id,
            topic,
            rounds,
            engines,
            cfg=cfg,
            running_tasks=running_tasks,
            chat_prefs=chat_prefs,
            roundtables=roundtables,
        ),
        continue_roundtable=continue_rt,
        close_roundtable=close_rt,
        thread_id=thread_id,
    )
