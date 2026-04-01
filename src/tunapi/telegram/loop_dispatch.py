"""Dispatch functions extracted from the Telegram event loop.

All functions take a :class:`TelegramLoopContext` as their first
parameter, replacing the closure captures from ``run_main_loop``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING, Any

import anyio

from ..commands import list_command_ids  # noqa: F401 — also accessed via _loop_mod for monkeypatch
from ..context import RunContext
from ..directives import DirectiveError
from ..logging import get_logger
from ..model import EngineId, ResumeToken
from ..progress import ProgressTracker
from ..transport import MessageRef, SendOptions
from ..transport_runtime import ResolvedMessage
from .bridge import CANCEL_CALLBACK_DATA, TelegramBridgeConfig
from .builtin_commands import (
    dispatch_builtin_command as _dispatch_builtin_command,
    dispatch_rt_command as _dispatch_rt_command,
)
from .commands.cancel import handle_callback_cancel, handle_cancel
from .commands.file_transfer import FILE_PUT_USAGE
from .commands.handlers import (
    dispatch_command,
    handle_chat_new_command,
    handle_file_put_default,
    handle_new_command,
    get_reserved_commands,
    run_engine,
    save_file_put,
    should_show_resume_line,
)
from .commands.reply import make_reply
from .context import _merge_topic_context, _usage_ctx_set, _usage_topic
from .engine_defaults import resolve_engine_for_message
from .forward_coalescing import (
    PendingPrompt as _PendingPrompt,
    format_forwarded_prompt as _format_forwarded_prompt,
    forward_key as _forward_key,
)
from .loop_state import (
    TelegramCommandContext,
    TelegramLoopContext,
    TelegramMsgContext,
    _SEEN_MESSAGES_LIMIT,
    _SEEN_UPDATES_LIMIT,
    chat_session_key as _chat_session_key,
    classify_message as _classify_message,
    resolve_engine_run_options as _resolve_engine_run_options,
)
from .topics import (
    _maybe_rename_topic,
    _topic_key,
    _topics_chat_allowed,
    _topics_chat_project,
)
from .trigger_mode import resolve_trigger_mode, should_trigger_run
from .types import (
    TelegramCallbackQuery,
    TelegramIncomingMessage,
    TelegramIncomingUpdate,
)
import importlib as _importlib

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

_handle_file_put_default = handle_file_put_default


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def resolve_topic_key(
    ctx: TelegramLoopContext,
    msg: TelegramIncomingMessage,
) -> tuple[int, int] | None:
    if ctx.state.topic_store is None:
        return None
    return _topic_key(msg, ctx.cfg, scope_chat_ids=ctx.state.topics_chat_ids)


def _build_upload_prompt(base: str, annotation: str) -> str:
    if base and base.strip():
        return f"{base}\n\n{annotation}"
    return annotation


def wrap_on_thread_known(
    ctx: TelegramLoopContext,
    base_cb: Callable[[ResumeToken, anyio.Event], Awaitable[None]] | None,
    topic_key: tuple[int, int] | None,
    chat_session_key: tuple[int, int | None] | None,
) -> Callable[[ResumeToken, anyio.Event], Awaitable[None]] | None:
    state = ctx.state
    if base_cb is None and topic_key is None and chat_session_key is None:
        return None

    async def _wrapped(token: ResumeToken, done: anyio.Event) -> None:
        if base_cb is not None:
            await base_cb(token, done)
        if state.topic_store is not None and topic_key is not None:
            await state.topic_store.set_session_resume(
                topic_key[0], topic_key[1], token
            )
        if state.chat_session_store is not None and chat_session_key is not None:
            await state.chat_session_store.set_session_resume(
                chat_session_key[0], chat_session_key[1], token
            )

    return _wrapped


# ---------------------------------------------------------------------------
# Context / prompt resolution
# ---------------------------------------------------------------------------


async def resolve_prompt_message(
    ctx: TelegramLoopContext,
    msg: TelegramIncomingMessage,
    text: str,
    ambient_context: RunContext | None,
) -> ResolvedMessage | None:
    reply = make_reply(ctx.cfg, msg)
    try:
        resolved = ctx.cfg.runtime.resolve_message(
            text=text,
            reply_text=msg.reply_to_text,
            ambient_context=ambient_context,
            chat_id=msg.chat_id,
        )
    except DirectiveError as exc:
        await reply(text=f"error:\n{exc}")
        return None
    topic_key = resolve_topic_key(ctx, msg)
    chat_project = (
        _topics_chat_project(ctx.cfg, msg.chat_id) if ctx.cfg.topics.enabled else None
    )
    _, ok = await ensure_topic_context(
        ctx,
        resolved=resolved,
        ambient_context=ambient_context,
        topic_key=topic_key,
        chat_project=chat_project,
        reply=reply,
    )
    if not ok:
        return None
    return resolved


async def resolve_engine_defaults(
    ctx: TelegramLoopContext,
    *,
    explicit_engine: EngineId | None,
    context: RunContext | None,
    chat_id: int,
    topic_key: tuple[int, int] | None,
) -> Any:
    return await resolve_engine_for_message(
        runtime=ctx.cfg.runtime,
        context=context,
        explicit_engine=explicit_engine,
        chat_id=chat_id,
        topic_key=topic_key,
        topic_store=ctx.state.topic_store,
        chat_prefs=ctx.state.chat_prefs,
    )


async def ensure_topic_context(
    ctx: TelegramLoopContext,
    *,
    resolved: ResolvedMessage,
    ambient_context: RunContext | None,
    topic_key: tuple[int, int] | None,
    chat_project: str | None,
    reply: Callable[..., Awaitable[None]],
) -> tuple[RunContext | None, bool]:
    state = ctx.state
    effective_context = ambient_context
    if (
        state.topic_store is not None
        and topic_key is not None
        and resolved.context is not None
        and resolved.context_source == "directives"
    ):
        await state.topic_store.set_context(*topic_key, resolved.context)
        await _maybe_rename_topic(
            ctx.cfg,
            state.topic_store,
            chat_id=topic_key[0],
            thread_id=topic_key[1],
            context=resolved.context,
        )
        effective_context = resolved.context
    if (
        state.topic_store is not None
        and topic_key is not None
        and effective_context is None
        and resolved.context_source not in {"directives", "reply_ctx"}
    ):
        await reply(
            text="this topic isn't bound to a project yet.\n"
            f"{_usage_ctx_set(chat_project=chat_project)} or "
            f"{_usage_topic(chat_project=chat_project)}",
        )
        return effective_context, False
    return effective_context, True


async def build_message_context(
    ctx: TelegramLoopContext,
    msg: TelegramIncomingMessage,
) -> TelegramMsgContext:
    state = ctx.state
    cfg = ctx.cfg
    chat_id = msg.chat_id
    reply_id = msg.reply_to_message_id
    reply_ref = (
        MessageRef(channel_id=chat_id, message_id=reply_id)
        if reply_id is not None
        else None
    )
    topic_key = resolve_topic_key(ctx, msg)
    chat_session_key = _chat_session_key(msg, store=state.chat_session_store)
    stateful_mode = topic_key is not None or chat_session_key is not None
    chat_project = _topics_chat_project(cfg, chat_id) if cfg.topics.enabled else None
    bound_context = (
        await state.topic_store.get_context(*topic_key)
        if state.topic_store is not None and topic_key is not None
        else None
    )
    chat_bound_context = None
    if state.chat_prefs is not None:
        chat_bound_context = await state.chat_prefs.get_context(chat_id)
    if bound_context is not None:
        ambient_context = _merge_topic_context(
            chat_project=chat_project, bound=bound_context
        )
    elif chat_bound_context is not None:
        ambient_context = chat_bound_context
    else:
        ambient_context = _merge_topic_context(chat_project=chat_project, bound=None)
    return TelegramMsgContext(
        chat_id=chat_id,
        thread_id=msg.thread_id,
        reply_id=reply_id,
        reply_ref=reply_ref,
        topic_key=topic_key,
        chat_session_key=chat_session_key,
        stateful_mode=stateful_mode,
        chat_project=chat_project,
        ambient_context=ambient_context,
    )


# ---------------------------------------------------------------------------
# Engine execution
# ---------------------------------------------------------------------------


async def run_job(
    ctx: TelegramLoopContext,
    chat_id: int,
    user_msg_id: int,
    text: str,
    resume_token: ResumeToken | None,
    context: RunContext | None,
    thread_id: int | None = None,
    chat_session_key: tuple[int, int | None] | None = None,
    reply_ref: MessageRef | None = None,
    on_thread_known: Callable[[ResumeToken, anyio.Event], Awaitable[None]]
    | None = None,
    engine_override: EngineId | None = None,
    progress_ref: MessageRef | None = None,
) -> None:
    cfg = ctx.cfg
    state = ctx.state
    topic_key = (
        (chat_id, thread_id)
        if state.topic_store is not None
        and thread_id is not None
        and _topics_chat_allowed(cfg, chat_id, scope_chat_ids=state.topics_chat_ids)
        else None
    )
    stateful_mode = topic_key is not None or chat_session_key is not None
    show_resume_line = should_show_resume_line(
        show_resume_line=cfg.show_resume_line,
        stateful_mode=stateful_mode,
        context=context,
    )
    engine_for_overrides = (
        resume_token.engine
        if resume_token is not None
        else engine_override
        if engine_override is not None
        else cfg.runtime.resolve_engine(engine_override=None, context=context)
    )
    overrides_thread_id = topic_key[1] if topic_key is not None else None
    run_options = await _resolve_engine_run_options(
        chat_id,
        overrides_thread_id,
        engine_for_overrides,
        chat_prefs=state.chat_prefs,
        topic_store=state.topic_store,
    )
    await run_engine(
        exec_cfg=cfg.exec_cfg,
        runtime=cfg.runtime,
        running_tasks=state.running_tasks,
        chat_id=chat_id,
        user_msg_id=user_msg_id,
        text=text,
        resume_token=resume_token,
        context=context,
        reply_ref=reply_ref,
        on_thread_known=wrap_on_thread_known(
            ctx, on_thread_known, topic_key, chat_session_key
        ),
        engine_override=engine_override,
        thread_id=thread_id,
        show_resume_line=show_resume_line,
        progress_ref=progress_ref,
        run_options=run_options,
    )


async def dispatch_prompt_run(
    ctx: TelegramLoopContext,
    *,
    msg: TelegramIncomingMessage,
    prompt_text: str,
    resolved: ResolvedMessage,
    topic_key: tuple[int, int] | None,
    chat_session_key: tuple[int, int | None] | None,
    reply_ref: MessageRef | None,
    reply_id: int | None,
) -> None:
    cfg = ctx.cfg
    scheduler = ctx.scheduler
    chat_id = msg.chat_id
    user_msg_id = msg.message_id
    context = resolved.context
    engine_resolution = await resolve_engine_defaults(
        ctx,
        explicit_engine=resolved.engine_override,
        context=context,
        chat_id=chat_id,
        topic_key=topic_key,
    )
    engine_override = engine_resolution.engine
    resume_decision = await ctx.resume_resolver.resolve(
        resume_token=resolved.resume_token,
        reply_id=reply_id,
        chat_id=chat_id,
        user_msg_id=user_msg_id,
        thread_id=msg.thread_id,
        chat_session_key=chat_session_key,
        topic_key=topic_key,
        engine_for_session=engine_resolution.engine,
        prompt_text=prompt_text,
    )
    if resume_decision.handled_by_running_task:
        return
    resume_token = resume_decision.resume_token
    if resume_token is None:
        await run_job(
            ctx,
            chat_id,
            user_msg_id,
            prompt_text,
            None,
            context,
            msg.thread_id,
            chat_session_key,
            reply_ref,
            scheduler.note_thread_known,
            engine_override,
        )
        return
    progress_ref = await _send_queued_progress(
        cfg,
        chat_id=chat_id,
        user_msg_id=user_msg_id,
        thread_id=msg.thread_id,
        resume_token=resume_token,
        context=context,
    )
    await scheduler.enqueue_resume(
        chat_id,
        user_msg_id,
        prompt_text,
        resume_token,
        context,
        msg.thread_id,
        chat_session_key,
        progress_ref,
    )


async def _send_queued_progress(
    cfg: TelegramBridgeConfig,
    *,
    chat_id: int,
    user_msg_id: int,
    thread_id: int | None,
    resume_token: ResumeToken,
    context: RunContext | None,
) -> MessageRef | None:
    tracker = ProgressTracker(engine=resume_token.engine)
    tracker.set_resume(resume_token)
    context_line = cfg.runtime.format_context_line(context)
    state = tracker.snapshot(context_line=context_line)
    message = cfg.exec_cfg.presenter.render_progress(
        state, elapsed_s=0.0, label="queued"
    )
    reply_ref = MessageRef(
        channel_id=chat_id, message_id=user_msg_id, thread_id=thread_id
    )
    return await cfg.exec_cfg.transport.send(
        channel_id=chat_id,
        message=message,
        options=SendOptions(reply_to=reply_ref, notify=False, thread_id=thread_id),
    )


async def run_prompt_from_upload(
    ctx: TelegramLoopContext,
    msg: TelegramIncomingMessage,
    prompt_text: str,
    resolved: ResolvedMessage,
) -> None:
    reply_id = msg.reply_to_message_id
    reply_ref = (
        MessageRef(
            channel_id=msg.chat_id,
            message_id=msg.reply_to_message_id,
            thread_id=msg.thread_id,
        )
        if msg.reply_to_message_id is not None
        else None
    )
    chat_session_key_val = _chat_session_key(msg, store=ctx.state.chat_session_store)
    topic_key = resolve_topic_key(ctx, msg)
    await dispatch_prompt_run(
        ctx,
        msg=msg,
        prompt_text=prompt_text,
        resolved=resolved,
        topic_key=topic_key,
        chat_session_key=chat_session_key_val,
        reply_ref=reply_ref,
        reply_id=reply_id,
    )


async def _dispatch_pending_prompt(
    ctx: TelegramLoopContext,
    pending: _PendingPrompt,
) -> None:
    msg = pending.msg
    reply = make_reply(ctx.cfg, msg)
    try:
        resolved = ctx.cfg.runtime.resolve_message(
            text=pending.text,
            reply_text=msg.reply_to_text,
            ambient_context=pending.ambient_context,
            chat_id=msg.chat_id,
        )
    except DirectiveError as exc:
        await reply(text=f"error:\n{exc}")
        return
    if pending.is_voice_transcribed:
        resolved = ResolvedMessage(
            prompt=f"(voice transcribed) {resolved.prompt}",
            resume_token=resolved.resume_token,
            engine_override=resolved.engine_override,
            context=resolved.context,
            context_source=resolved.context_source,
        )
    prompt_text = resolved.prompt
    if pending.forwards:
        forwarded = [
            text for _, text in sorted(pending.forwards, key=lambda item: item[0])
        ]
        prompt_text = _format_forwarded_prompt(forwarded, prompt_text)
    _effective_context, ok = await ensure_topic_context(
        ctx,
        resolved=resolved,
        ambient_context=pending.ambient_context,
        topic_key=pending.topic_key,
        chat_project=pending.chat_project,
        reply=reply,
    )
    if not ok:
        return
    await dispatch_prompt_run(
        ctx,
        msg=msg,
        prompt_text=prompt_text,
        resolved=resolved,
        topic_key=pending.topic_key,
        chat_session_key=pending.chat_session_key,
        reply_ref=pending.reply_ref,
        reply_id=pending.reply_id,
    )


async def handle_prompt_upload(
    ctx: TelegramLoopContext,
    msg: TelegramIncomingMessage,
    caption_text: str,
    ambient_context: RunContext | None,
    topic_store: Any,
) -> None:
    resolved = await resolve_prompt_message(ctx, msg, caption_text, ambient_context)
    if resolved is None:
        return
    saved = await save_file_put(ctx.cfg, msg, "", resolved.context, topic_store)
    if saved is None:
        return
    annotation = f"[uploaded file: {saved.rel_path.as_posix()}]"
    prompt = _build_upload_prompt(resolved.prompt, annotation)
    await run_prompt_from_upload(ctx, msg, prompt, resolved)


# ---------------------------------------------------------------------------
# Message / update routing
# ---------------------------------------------------------------------------


async def route_message(
    ctx: TelegramLoopContext,
    msg: TelegramIncomingMessage,
) -> None:
    cfg = ctx.cfg
    state = ctx.state
    tg = ctx.tg
    scheduler = ctx.scheduler

    reply = make_reply(cfg, msg)
    classification = _classify_message(msg, files_enabled=cfg.files.enabled)
    text = classification.text
    is_voice_transcribed = False

    if classification.is_forward_candidate:
        ctx.forward_coalescer.attach_forward(msg)
        return
    forward_key = _forward_key(msg)

    if classification.is_media_group_document:
        ctx.media_group_buffer.add(msg)
        return

    mctx = await build_message_context(ctx, msg)
    chat_id = mctx.chat_id
    reply_id = mctx.reply_id
    reply_ref = mctx.reply_ref
    topic_key = mctx.topic_key
    chat_session_key = mctx.chat_session_key
    stateful_mode = mctx.stateful_mode
    chat_project = mctx.chat_project
    ambient_context = mctx.ambient_context

    if classification.is_cancel:
        tg.start_soon(handle_cancel, cfg, msg, state.running_tasks, scheduler)
        return

    command_id = classification.command_id
    args_text = classification.args_text

    if command_id == "new":
        ctx.forward_coalescer.cancel(forward_key)
        if state.topic_store is not None and topic_key is not None:
            tg.start_soon(
                partial(
                    handle_new_command,
                    cfg,
                    msg,
                    state.topic_store,
                    resolved_scope=state.resolved_topics_scope,
                    scope_chat_ids=state.topics_chat_ids,
                )
            )
            return
        if state.chat_session_store is not None:
            tg.start_soon(
                handle_chat_new_command,
                cfg,
                msg,
                state.chat_session_store,
                chat_session_key,
            )
            return
        if state.topic_store is not None:
            tg.start_soon(
                partial(
                    handle_new_command,
                    cfg,
                    msg,
                    state.topic_store,
                    resolved_scope=state.resolved_topics_scope,
                    scope_chat_ids=state.topics_chat_ids,
                )
            )
            return

    if command_id == "rt":
        if state.roundtable_store is None:
            tg.start_soon(partial(reply, text="Roundtable store not initialised."))
        else:
            tg.start_soon(_dispatch_rt_command, ctx, msg, args_text)
        return

    if command_id is not None and _dispatch_builtin_command(
        ctx=TelegramCommandContext(
            cfg=cfg,
            msg=msg,
            args_text=args_text,
            ambient_context=ambient_context,
            topic_store=state.topic_store,
            chat_prefs=state.chat_prefs,
            resolved_scope=state.resolved_topics_scope,
            scope_chat_ids=state.topics_chat_ids,
            reply=reply,
            task_group=tg,
        ),
        command_id=command_id,
    ):
        return

    trigger_mode = await resolve_trigger_mode(
        chat_id=chat_id,
        thread_id=msg.thread_id,
        chat_prefs=state.chat_prefs,
        topic_store=state.topic_store,
    )
    if trigger_mode == "mentions" and not should_trigger_run(
        msg,
        bot_username=state.bot_username,
        runtime=cfg.runtime,
        command_ids=state.command_ids,
        reserved_chat_commands=state.reserved_chat_commands,
    ):
        return

    if msg.voice is not None:
        # Lazy import to avoid circular dependency and allow test monkeypatch on loop module
        _loop_mod = _importlib.import_module("tunapi.telegram.loop")
        text = await _loop_mod.transcribe_voice(
            bot=cfg.bot,
            msg=msg,
            enabled=cfg.voice_transcription,
            model=cfg.voice_transcription_model,
            max_bytes=cfg.voice_max_bytes,
            reply=reply,
            base_url=cfg.voice_transcription_base_url,
            api_key=cfg.voice_transcription_api_key,
        )
        if text is None:
            return
        is_voice_transcribed = True

    if msg.document is not None:
        if cfg.files.enabled and cfg.files.auto_put:
            caption_text = text.strip()
            if cfg.files.auto_put_mode == "prompt" and caption_text:
                tg.start_soon(
                    handle_prompt_upload,
                    ctx,
                    msg,
                    caption_text,
                    ambient_context,
                    state.topic_store,
                )
            elif not caption_text:
                _loop_mod = _importlib.import_module("tunapi.telegram.loop")
                tg.start_soon(
                    _loop_mod._handle_file_put_default,
                    cfg,
                    msg,
                    ambient_context,
                    state.topic_store,
                )
            else:
                tg.start_soon(partial(reply, text=FILE_PUT_USAGE))
        elif cfg.files.enabled:
            tg.start_soon(partial(reply, text=FILE_PUT_USAGE))
        return

    if command_id is not None and command_id not in state.reserved_commands:
        if command_id not in state.command_ids:
            _loop_mod = _importlib.import_module("tunapi.telegram.loop")
            allowlist = cfg.runtime.allowlist
            state.command_ids = {
                cid.lower() for cid in _loop_mod.list_command_ids(allowlist=allowlist)
            }
            state.reserved_commands = get_reserved_commands(cfg.runtime)
        if command_id in state.command_ids:
            engine_resolution = await resolve_engine_defaults(
                ctx,
                explicit_engine=None,
                context=ambient_context,
                chat_id=chat_id,
                topic_key=topic_key,
            )
            default_engine_override = (
                engine_resolution.engine
                if engine_resolution.source
                in {"directive", "topic_default", "chat_default"}
                else None
            )
            overrides_thread_id = topic_key[1] if topic_key is not None else None
            engine_overrides_resolver = partial(
                _resolve_engine_run_options,
                chat_id,
                overrides_thread_id,
                chat_prefs=state.chat_prefs,
                topic_store=state.topic_store,
            )
            tg.start_soon(
                dispatch_command,
                cfg,
                msg,
                text,
                command_id,
                args_text,
                state.running_tasks,
                scheduler,
                wrap_on_thread_known(
                    ctx,
                    scheduler.note_thread_known,
                    topic_key,
                    chat_session_key,
                ),
                stateful_mode,
                default_engine_override,
                engine_overrides_resolver,
            )
            return

    pending = _PendingPrompt(
        msg=msg,
        text=text,
        ambient_context=ambient_context,
        chat_project=chat_project,
        topic_key=topic_key,
        chat_session_key=chat_session_key,
        reply_ref=reply_ref,
        reply_id=reply_id,
        is_voice_transcribed=is_voice_transcribed,
        forwards=[],
    )
    if reply_id is not None and state.running_tasks.get(
        MessageRef(channel_id=chat_id, message_id=reply_id)
    ):
        logger.debug(
            "forward.prompt.bypass",
            chat_id=chat_id,
            thread_id=msg.thread_id,
            sender_id=msg.sender_id,
            message_id=msg.message_id,
            reason="reply_resume",
        )
        tg.start_soon(_dispatch_pending_prompt, ctx, pending)
        return
    ctx.forward_coalescer.schedule(pending)


async def route_update(
    ctx: TelegramLoopContext,
    update: TelegramIncomingUpdate,
    allowed_user_ids: set[int],
) -> None:
    state = ctx.state
    tg = ctx.tg
    scheduler = ctx.scheduler
    cfg = ctx.cfg

    if allowed_user_ids:
        sender_id = update.sender_id
        if sender_id is None or sender_id not in allowed_user_ids:
            logger.debug(
                "update.ignored",
                reason="sender_not_allowed",
                chat_id=update.chat_id,
                sender_id=sender_id,
            )
            return

    if update.update_id is not None:
        update_id = update.update_id
        if update_id in state.seen_update_ids:
            logger.debug(
                "update.ignored",
                reason="duplicate_update",
                update_id=update_id,
                chat_id=update.chat_id,
                sender_id=update.sender_id,
            )
            return
        state.seen_update_ids.add(update_id)
        state.seen_update_order.append(update_id)
        if len(state.seen_update_order) > _SEEN_UPDATES_LIMIT:
            oldest_update_id = state.seen_update_order.popleft()
            state.seen_update_ids.discard(oldest_update_id)
    elif isinstance(update, TelegramIncomingMessage):
        key = (update.chat_id, update.message_id)
        if key in state.seen_message_keys:
            logger.debug(
                "update.ignored",
                reason="duplicate_message",
                chat_id=update.chat_id,
                message_id=update.message_id,
                sender_id=update.sender_id,
            )
            return
        state.seen_message_keys.add(key)
        state.seen_messages_order.append(key)
        if len(state.seen_messages_order) > _SEEN_MESSAGES_LIMIT:
            oldest = state.seen_messages_order.popleft()
            state.seen_message_keys.discard(oldest)

    if isinstance(update, TelegramCallbackQuery):
        if update.data == CANCEL_CALLBACK_DATA:
            tg.start_soon(
                handle_callback_cancel, cfg, update, state.running_tasks, scheduler
            )
        else:
            tg.start_soon(cfg.bot.answer_callback_query, update.callback_query_id)
        return

    await route_message(ctx, update)
