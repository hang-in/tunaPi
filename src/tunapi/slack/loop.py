"""Main Socket Mode event loop for the Slack transport."""

from __future__ import annotations

import contextlib
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import anyio

from ..core import lifecycle
from ..journal import Journal, PendingRunLedger, build_handoff_preamble, make_run_id
from ..logging import bind_run_context, get_logger
from ..model import ResumeToken
from ..runner_bridge import IncomingMessage, handle_message
from ..transport import MessageRef, RenderedMessage
from .bridge import CANCEL_EMOJI, SlackBridgeConfig
from .chat_prefs import ChatPrefsStore
from .chat_sessions import ChatSessionStore
from .commands import (
    handle_cancel,
    handle_help,
    handle_model,
    handle_persona,
    handle_project,
    handle_status,
    handle_trigger,
    parse_command,
)
from .parsing import SlackMessageEvent, SlackReactionEvent, parse_envelope
from .trigger_mode import resolve_trigger_mode, should_trigger, strip_mention

if TYPE_CHECKING:
    from ..runner_bridge import RunningTasks

logger = get_logger(__name__)

# Callback type for sending a message to a channel.
type _SendFn = Callable[[RenderedMessage], Awaitable[None]]

_CONFIG_DIR = Path.home() / ".tunapi"
_SHUTDOWN_STATE_FILE = _CONFIG_DIR / "slack_last_shutdown.json"


async def _send_startup(cfg: SlackBridgeConfig) -> None:
    if cfg.channel_id is None:
        logger.info("slack.startup_skipped", reason="no channel_id")
        return
    msg = RenderedMessage(text=cfg.startup_msg)
    await cfg.exec_cfg.transport.send(channel_id=cfg.channel_id, message=msg)
    logger.info("slack.startup_sent")


async def _send_to_channel(
    cfg: SlackBridgeConfig,
    channel_id: str,
    message: RenderedMessage,
) -> None:
    await cfg.exec_cfg.transport.send(channel_id=channel_id, message=message)


async def _handle_cancel_reaction(
    reaction: SlackReactionEvent,
    running_tasks: RunningTasks,
) -> None:
    if reaction.emoji != CANCEL_EMOJI:
        return

    # Cancel running task
    for ref, task in list(running_tasks.items()):
        if str(ref.message_id) == reaction.item_ts:
            logger.info(
                "slack.cancel_by_reaction",
                ts=reaction.item_ts,
                user_id=reaction.user_id,
            )
            task.cancel_requested.set()
            return


async def _try_dispatch_command(
    msg: SlackMessageEvent,
    cfg: SlackBridgeConfig,
    running_tasks: RunningTasks,
    sessions: ChatSessionStore,
    chat_prefs: ChatPrefsStore | None,
    send: _SendFn,
    journal: Journal | None = None,
) -> bool:
    """Handle slash/bang commands. Returns True if a command was dispatched."""
    cmd, args = parse_command(msg.text)
    if cmd is None:
        return False

    runtime = cfg.runtime

    match cmd:
        case "new":
            await sessions.clear(msg.channel_id)
            if journal:
                await journal.mark_reset(msg.channel_id)
            await send(RenderedMessage(text="새 대화를 시작합니다."))
        case "help":
            await handle_help(runtime=runtime, send=send)
        case "model":
            await handle_model(
                args,
                channel_id=msg.channel_id,
                runtime=runtime,
                chat_prefs=chat_prefs,
                send=send,
            )
        case "trigger":
            await handle_trigger(
                args,
                channel_id=msg.channel_id,
                chat_prefs=chat_prefs,
                send=send,
            )
        case "project":
            await handle_project(
                args,
                channel_id=msg.channel_id,
                runtime=runtime,
                chat_prefs=chat_prefs,
                projects_root=cfg.projects_root,
                send=send,
            )
        case "persona":
            await handle_persona(
                args,
                chat_prefs=chat_prefs,
                send=send,
            )
        case "status":
            has_session = await sessions.has_any(msg.channel_id)
            await handle_status(
                channel_id=msg.channel_id,
                runtime=runtime,
                chat_prefs=chat_prefs,
                session_engine=None,
                has_session=has_session,
                send=send,
            )
        case "cancel":
            await handle_cancel(
                channel_id=msg.channel_id,
                running_tasks=running_tasks,
                send=send,
            )
        case _:
            return False

    return True


async def _run_engine(
    prompt_text: str,
    msg: SlackMessageEvent,
    cfg: SlackBridgeConfig,
    running_tasks: RunningTasks,
    sessions: ChatSessionStore,
    chat_prefs: ChatPrefsStore | None,
    send: _SendFn,
    journal: Journal | None = None,
    ledger: PendingRunLedger | None = None,
) -> None:
    runtime = cfg.runtime

    # -- Resolve engine/context --
    ambient_context = None
    if chat_prefs:
        ambient_context = await chat_prefs.get_context(msg.channel_id)

    resolved = runtime.resolve_message(
        text=prompt_text,
        reply_text=None,
        ambient_context=ambient_context,
        chat_id=msg.channel_id,
    )

    context = resolved.context
    engine_override = resolved.engine_override
    if engine_override is None and chat_prefs:
        pref_engine = await chat_prefs.get_default_engine(msg.channel_id)
        if pref_engine:
            engine_override = pref_engine

    engine = runtime.resolve_engine(
        engine_override=engine_override,
        context=context,
    )

    # -- Session handling --
    resume_token: ResumeToken | None = None
    if cfg.session_mode == "chat":
        resume_token = await sessions.get(msg.channel_id, engine)

    effective_resume = resolved.resume_token or resume_token

    resolved_runner = runtime.resolve_runner(
        resume_token=effective_resume,
        engine_override=engine,
    )

    if resolved_runner.issue:
        logger.warning("slack.runner_unavailable", issue=resolved_runner.issue)
        await send(RenderedMessage(text=f"⚠️ {resolved_runner.issue}"))
        return

    context_line = runtime.format_context_line(context)
    try:
        cwd = runtime.resolve_run_cwd(context)
    except Exception as exc:  # noqa: BLE001
        await send(RenderedMessage(text=f"⚠️ {exc}"))
        return
    if cwd:
        bind_run_context(project=context.project if context else None)

    # Thread handling
    reply_to = MessageRef(
        channel_id=msg.channel_id,
        message_id=msg.ts,
        thread_id=msg.thread_ts or msg.ts,
    )

    # -- Handoff preamble (when resume token is absent) --
    final_prompt = resolved.prompt
    if effective_resume is None and journal is not None and final_prompt:
        with contextlib.suppress(Exception):
            j_entries = await journal.recent_entries(msg.channel_id, limit=50)
            # Cross-transport fallback: if no entries for this channel,
            # check all channels for recent work
            if not j_entries:
                j_entries = await journal.recent_entries_global(limit=30)
            if j_entries:
                preamble = build_handoff_preamble(
                    j_entries,
                    old_engine=j_entries[-1].engine,
                    reason="engine_change"
                    if resume_token is None
                    else "resume_expired",
                )
                if preamble:
                    final_prompt = f"{preamble}\n{final_prompt}"

    incoming = IncomingMessage(
        channel_id=msg.channel_id,
        message_id=msg.ts,
        text=final_prompt,
        reply_to=reply_to,
        thread_id=msg.thread_ts or msg.ts,
    )

    async def on_thread_known(token: ResumeToken, done: anyio.Event) -> None:
        if cfg.session_mode == "chat":
            await sessions.set(msg.channel_id, token)

    j_run_id = make_run_id(msg.channel_id, msg.ts) if journal else None

    try:
        await handle_message(
            cfg.exec_cfg,
            runner=resolved_runner.runner,
            incoming=incoming,
            resume_token=effective_resume,
            context=context,
            context_line=context_line,
            strip_resume_line=runtime.is_resume_line,
            running_tasks=running_tasks,
            on_thread_known=on_thread_known,
            journal=journal,
            run_id=j_run_id,
            ledger=ledger,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "slack.dispatch_error",
            error=str(exc),
            error_type=exc.__class__.__name__,
            channel_id=msg.channel_id,
        )


async def _dispatch_message(
    msg: SlackMessageEvent,
    cfg: SlackBridgeConfig,
    running_tasks: RunningTasks,
    sessions: ChatSessionStore,
    chat_prefs: ChatPrefsStore | None,
    journal: Journal | None = None,
    ledger: PendingRunLedger | None = None,
) -> None:
    async def send(message: RenderedMessage) -> None:
        await _send_to_channel(cfg, msg.channel_id, message)

    # 1. Command handling
    if await _try_dispatch_command(
        msg, cfg, running_tasks, sessions, chat_prefs, send, journal=journal
    ):
        return

    # 2. Trigger check & Strip mention
    trigger_mode = await resolve_trigger_mode(msg.channel_id, chat_prefs)
    if not should_trigger(msg, bot_user_id=cfg.bot_user_id, trigger_mode=trigger_mode):
        return

    prompt_text = strip_mention(msg.text, cfg.bot_user_id)
    if not prompt_text:
        return

    # 3. Engine execution
    await _run_engine(
        prompt_text,
        msg,
        cfg,
        running_tasks,
        sessions,
        chat_prefs,
        send,
        journal=journal,
        ledger=ledger,
    )


async def run_main_loop(
    cfg: SlackBridgeConfig,
    *,
    watch_config: bool = False,
    default_engine_override: str | None = None,
    transport_id: str = "slack",
    transport_config: object | None = None,
) -> None:
    """Main event loop: connect Socket Mode, dispatch messages."""
    await _send_startup(cfg)

    running_tasks: RunningTasks = {}
    sessions = ChatSessionStore(_CONFIG_DIR / "slack_sessions.json")
    chat_prefs = ChatPrefsStore(_CONFIG_DIR / "slack_prefs.json")
    journal = Journal(_CONFIG_DIR / "journals")
    ledger = PendingRunLedger(_CONFIG_DIR / "slack_pending_runs.json")
    heartbeat_path = _CONFIG_DIR / "slack_heartbeat"

    async def _send_lifecycle_msg(ch_id: str, text: str) -> None:
        await cfg.exec_cfg.transport.send(
            channel_id=ch_id, message=RenderedMessage(text=text)
        )

    await lifecycle.detect_abnormal_termination(
        heartbeat_path=heartbeat_path,
        shutdown_state_path=_SHUTDOWN_STATE_FILE,
        log_prefix="slack",
    )
    await lifecycle.send_restart_notification(
        shutdown_state_path=_SHUTDOWN_STATE_FILE,
        channel_id=cfg.channel_id,
        send_fn=_send_lifecycle_msg,
    )
    await lifecycle.recover_pending_runs(
        journal=journal, ledger=ledger, send_fn=_send_lifecycle_msg
    )

    shutdown = anyio.Event()
    lifecycle.register_sigterm_handler(shutdown, log_prefix="slack")

    async with anyio.create_task_group() as dispatch_tg:
        dispatch_tg.start_soon(lifecycle.heartbeat_loop, heartbeat_path)
        async with cfg.bot.socket_mode_events() as events:
            async for envelope in events:
                if shutdown.is_set():
                    logger.info("slack.shutdown_ws_stop")
                    break

                update = parse_envelope(
                    envelope,
                    bot_user_id=cfg.bot_user_id,
                    allowed_channel_ids=cfg.allowed_channel_ids or None,
                    allowed_user_ids=cfg.allowed_user_ids or None,
                )
                if update is None:
                    continue

                if isinstance(update, SlackReactionEvent):
                    await _handle_cancel_reaction(update, running_tasks)
                elif isinstance(update, SlackMessageEvent):
                    logger.info(
                        "slack.incoming",
                        channel_id=update.channel_id,
                        user_id=update.user_id,
                        text=update.text[:100],
                    )
                    dispatch_tg.start_soon(
                        _dispatch_message,
                        update,
                        cfg,
                        running_tasks,
                        sessions,
                        chat_prefs,
                        journal,
                        ledger,
                    )

        await lifecycle.graceful_drain(running_tasks, log_prefix="slack")
        lifecycle.save_shutdown_state(
            shutdown_state_path=_SHUTDOWN_STATE_FILE,
            is_sigterm=shutdown.is_set(),
            running_task_count=len(running_tasks),
        )
        lifecycle.cleanup_heartbeat(heartbeat_path)
