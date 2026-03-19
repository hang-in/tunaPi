"""Main WebSocket event loop for the Mattermost transport."""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import anyio

from ..core import lifecycle
from ..journal import Journal, PendingRunLedger, build_handoff_preamble, make_run_id
from ..logging import bind_run_context, get_logger
from ..model import ResumeToken
from ..runner_bridge import IncomingMessage, handle_message
from ..transport import MessageRef, RenderedMessage
from .bridge import CANCEL_EMOJI, MattermostBridgeConfig
from .chat_prefs import ChatPrefsStore
from .chat_sessions import ChatSessionStore
from .commands import (
    handle_cancel,
    handle_help,
    handle_model,
    handle_persona,
    handle_project,
    handle_rt,
    handle_status,
    handle_trigger,
    parse_command,
)
from .roundtable import (
    RoundtableSession,
    RoundtableStore,
    run_followup_round,
    run_roundtable,
)
from .files import handle_file_get, handle_file_put
from .parsing import parse_ws_event
from .trigger_mode import resolve_trigger_mode, should_trigger, strip_mention
from .types import MattermostIncomingMessage, MattermostReactionEvent
from .voice import is_audio_file, transcribe_audio

if TYPE_CHECKING:
    from ..runner_bridge import RunningTasks

logger = get_logger(__name__)

# Callback type for sending a message to a channel.
type _SendFn = Callable[[RenderedMessage], Awaitable[None]]

_CONFIG_DIR = Path.home() / ".tunapi"
_SHUTDOWN_STATE_FILE = _CONFIG_DIR / "last_shutdown.json"


def _resolve_upload_dir(cfg: MattermostBridgeConfig, channel_id: str) -> Path:
    """Resolve the upload target directory for a channel."""
    context = cfg.runtime.default_context_for_chat(channel_id)
    cwd = cfg.runtime.resolve_run_cwd(context)
    root = cwd or Path.cwd()
    return root / cfg.files_uploads_dir


async def _put_files(
    cfg: MattermostBridgeConfig,
    channel_id: str,
    file_ids: list[str],
) -> list:
    """Upload files to the project directory. Returns list of FileResult."""
    target_dir = _resolve_upload_dir(cfg, channel_id)
    return await handle_file_put(
        client=cfg.bot,
        channel_id=channel_id,
        file_ids=file_ids,
        target_dir=target_dir,
        deny_globs=cfg.files_deny_globs,
        max_bytes=cfg.files_max_upload_bytes,
    )


async def _send_startup(cfg: MattermostBridgeConfig) -> None:
    msg = RenderedMessage(text=cfg.startup_msg)
    await cfg.exec_cfg.transport.send(channel_id=cfg.channel_id, message=msg)
    logger.info("mattermost.startup_sent")


async def _send_to_channel(
    cfg: MattermostBridgeConfig,
    channel_id: str,
    message: RenderedMessage,
) -> None:
    await cfg.exec_cfg.transport.send(channel_id=channel_id, message=message)


async def _handle_cancel_reaction(
    reaction: MattermostReactionEvent,
    running_tasks: RunningTasks,
    roundtables: RoundtableStore | None = None,
) -> None:
    if reaction.emoji_name != CANCEL_EMOJI:
        return
    # Cancel roundtable session if 🛑 on header post
    if roundtables:
        session = roundtables.get(reaction.post_id)
        if session is not None:
            logger.info(
                "roundtable.cancel_by_reaction",
                thread_id=session.thread_id,
                user_id=reaction.user_id,
            )
            session.cancel_event.set()
            return
    # Cancel running task
    for ref, task in list(running_tasks.items()):
        if str(ref.message_id) == reaction.post_id:
            logger.info(
                "mattermost.cancel_by_reaction",
                post_id=reaction.post_id,
                user_id=reaction.user_id,
            )
            task.cancel_requested.set()
            return


async def _handle_voice(
    msg: MattermostIncomingMessage,
    cfg: MattermostBridgeConfig,
) -> str | None:
    """If the message has an audio attachment, transcribe it and return text."""
    if not cfg.voice_enabled or not msg.file_ids:
        return None

    for file_id in msg.file_ids:
        info = await cfg.bot._client.get_file_info(file_id)
        if info is None:
            continue
        if not is_audio_file(info.mime_type):
            continue
        if info.size > cfg.voice_max_bytes:
            logger.warning("voice.too_large", size=info.size, max=cfg.voice_max_bytes)
            continue

        audio_data = await cfg.bot.get_file(file_id)
        if audio_data is None:
            continue

        text = await transcribe_audio(
            audio_data,
            info.name,
            model=cfg.voice_model,
            base_url=cfg.voice_base_url,
            api_key=cfg.voice_api_key,
        )
        if text:
            logger.info(
                "voice.transcribed", channel_id=msg.channel_id, length=len(text)
            )
            return text

    return None


async def _handle_file_command(
    args: str,
    msg: MattermostIncomingMessage,
    cfg: MattermostBridgeConfig,
) -> bool:
    """Handle /file put or /file get. Returns True if handled."""
    if not cfg.files_enabled:
        await _send_to_channel(
            cfg,
            msg.channel_id,
            RenderedMessage(text="File transfer is disabled."),
        )
        return True

    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""
    subargs = parts[1] if len(parts) > 1 else ""

    if subcmd == "put":
        if not msg.file_ids:
            await _send_to_channel(
                cfg,
                msg.channel_id,
                RenderedMessage(text="Attach files to the message to upload."),
            )
            return True

        results = await _put_files(cfg, msg.channel_id, list(msg.file_ids))
        text = (
            "\n".join(f"- {r.message}" for r in results)
            if results
            else "No files processed."
        )
        await _send_to_channel(cfg, msg.channel_id, RenderedMessage(text=text))
        return True

    elif subcmd == "get":
        rel_path = subargs.strip()
        if not rel_path:
            await _send_to_channel(
                cfg,
                msg.channel_id,
                RenderedMessage(text="Usage: `/file get <path>`"),
            )
            return True

        upload_dir = _resolve_upload_dir(cfg, msg.channel_id)
        root = upload_dir.parent  # project root (upload_dir = root / uploads_dir)

        filename, error, content = await handle_file_get(
            client=cfg.bot,
            channel_id=msg.channel_id,
            rel_path=rel_path,
            root=root,
            deny_globs=cfg.files_deny_globs,
            max_bytes=cfg.files_max_download_bytes,
        )
        if error:
            await _send_to_channel(cfg, msg.channel_id, RenderedMessage(text=error))
            return True

        # Upload file and send as post with attachment
        file_info = await cfg.bot.upload_file(msg.channel_id, filename, content)
        if file_info:
            await cfg.bot.send_message(
                msg.channel_id,
                f"`{rel_path}`",
                file_ids=[file_info.id],
            )
        else:
            await _send_to_channel(
                cfg,
                msg.channel_id,
                RenderedMessage(text="Failed to upload file."),
            )
        return True

    else:
        await _send_to_channel(
            cfg,
            msg.channel_id,
            RenderedMessage(
                text="Usage: `/file put` (with attachments) or `/file get <path>`"
            ),
        )
        return True


_PERSONA_PREFIX_RE = re.compile(r"^@(\w+)\s+", re.UNICODE)


async def _resolve_persona_prefix(
    prompt: str, chat_prefs: ChatPrefsStore
) -> str | None:
    """If prompt starts with @persona_name, prepend the persona prompt.

    Returns the modified prompt, or None if no persona prefix was found.
    """
    m = _PERSONA_PREFIX_RE.match(prompt)
    if not m:
        return None
    name = m.group(1).lower()
    persona = await chat_prefs.get_persona(name)
    if persona is None:
        return None
    user_text = prompt[m.end() :]
    return f"[역할: {persona.name}]\n{persona.prompt}\n\n---\n\n{user_text}"


async def _start_roundtable(
    channel_id: str,
    topic: str,
    rounds: int,
    engines: list[str],
    *,
    cfg: MattermostBridgeConfig,
    running_tasks: RunningTasks,
    chat_prefs: ChatPrefsStore | None,
    roundtables: RoundtableStore,
) -> None:
    """Create a roundtable thread and run all rounds."""
    engines_display = ", ".join(f"`{e}`" for e in engines)
    rounds_display = f"{rounds} round{'s' if rounds > 1 else ''}"
    header = (
        f"**🔵 Roundtable**\n\n"
        f"**Topic:** {topic}\n"
        f"**Engines:** {engines_display} | **Rounds:** {rounds_display}\n\n"
        f"---"
    )
    ref = await cfg.exec_cfg.transport.send(
        channel_id=channel_id,
        message=RenderedMessage(text=header),
    )
    if ref is None:
        logger.error("roundtable.header_send_failed", channel_id=channel_id)
        return

    thread_id = str(ref.message_id)
    session = RoundtableSession(
        thread_id=thread_id,
        channel_id=channel_id,
        topic=topic,
        engines=engines,
        total_rounds=rounds,
    )
    roundtables.put(session)

    # Resolve ambient context (channel-bound project)
    ambient_context = None
    if chat_prefs:
        ambient_context = await chat_prefs.get_context(channel_id)

    logger.info(
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


@dataclass(slots=True)
class _ResolvedPrompt:
    """Result of prompt resolution before engine dispatch."""

    text: str
    file_context: str  # empty string if no files


async def _dispatch_rt_command(
    args: str,
    msg: MattermostIncomingMessage,
    cfg: MattermostBridgeConfig,
    running_tasks: RunningTasks,
    chat_prefs: ChatPrefsStore | None,
    roundtables: RoundtableStore | None,
    send: _SendFn,
) -> None:
    """Handle the !rt / /rt command, including follow-up detection."""
    continue_rt = None
    if msg.root_id and roundtables and roundtables.get_completed(msg.root_id):
        completed_session = roundtables.get_completed(msg.root_id)
        ambient_ctx = (
            await chat_prefs.get_context(msg.channel_id) if chat_prefs else None
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

    await handle_rt(
        args,
        runtime=cfg.runtime,
        send=send,
        start_roundtable=lambda topic, rounds, engines: _start_roundtable(
            msg.channel_id,
            topic,
            rounds,
            engines,
            cfg=cfg,
            running_tasks=running_tasks,
            chat_prefs=chat_prefs,
            roundtables=roundtables,
        ),
        continue_roundtable=continue_rt,
        thread_id=msg.root_id,
    )


async def _try_dispatch_command(
    msg: MattermostIncomingMessage,
    cfg: MattermostBridgeConfig,
    running_tasks: RunningTasks,
    sessions: ChatSessionStore,
    chat_prefs: ChatPrefsStore | None,
    roundtables: RoundtableStore | None,
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
        case "rt":
            await _dispatch_rt_command(
                args, msg, cfg, running_tasks, chat_prefs, roundtables, send
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
        case "file":
            await _handle_file_command(args, msg, cfg)
        case _:
            return False

    return True


async def _resolve_prompt(
    msg: MattermostIncomingMessage,
    cfg: MattermostBridgeConfig,
    chat_prefs: ChatPrefsStore | None,
    send: _SendFn,
) -> _ResolvedPrompt | None:
    """Resolve user input into a clean prompt text.

    Handles auto file upload, file+text attachment, voice transcription,
    trigger mode check, and @mention stripping.
    Returns None if the message should not be dispatched to an engine.
    """
    # -- Auto file put: attachment with no text → save to project --
    if msg.file_ids and not msg.text.strip() and cfg.files_enabled:
        results = await _put_files(cfg, msg.channel_id, list(msg.file_ids))
        text = (
            "\n".join(f"- {r.message}" for r in results)
            if results
            else "No files processed."
        )
        await send(RenderedMessage(text=text))
        return None

    # -- File + text: save files, add absolute paths to prompt --
    file_context = ""
    if msg.file_ids and msg.text.strip() and cfg.files_enabled:
        results = await _put_files(cfg, msg.channel_id, list(msg.file_ids))
        saved_paths = [str(r.path) for r in results if r.ok and r.path]
        if saved_paths:
            paths_str = ", ".join(f"`{p}`" for p in saved_paths)
            file_context = f"\n[Attached files saved to: {paths_str}]\n"

    # -- Voice transcription --
    voice_text = await _handle_voice(msg, cfg)
    prompt_text = voice_text or msg.text
    if file_context:
        prompt_text = f"{prompt_text}\n{file_context}"
    if not prompt_text:
        return None

    # -- Trigger mode check --
    trigger_mode = await resolve_trigger_mode(
        msg.channel_id,
        chat_prefs,
    )
    if not should_trigger(
        msg, bot_username=cfg.bot_username, trigger_mode=trigger_mode
    ):
        return None
    # Strip @mention from text
    prompt_text = strip_mention(prompt_text, cfg.bot_username)
    if not prompt_text:
        return None

    return _ResolvedPrompt(text=prompt_text, file_context=file_context)


async def _run_engine(
    resolved_prompt: _ResolvedPrompt,
    msg: MattermostIncomingMessage,
    cfg: MattermostBridgeConfig,
    running_tasks: RunningTasks,
    sessions: ChatSessionStore,
    chat_prefs: ChatPrefsStore | None,
    send: _SendFn,
    journal: Journal | None = None,
    ledger: PendingRunLedger | None = None,
) -> None:
    """Resolve engine/context and run the agent.

    Error boundary policy:
    - Runner unavailable (resolve_runner.issue): warn user via message, return
    - CWD resolution failure: warn user via message, return
    - handle_message() failure: log only (no user message)
    - Command handler errors: propagate (crash = bug in our code)
    """
    runtime = cfg.runtime

    # -- Resolve engine/context (use channel-bound project if set) --
    ambient_context = None
    if chat_prefs:
        ambient_context = await chat_prefs.get_context(msg.channel_id)

    resolved = runtime.resolve_message(
        text=resolved_prompt.text,
        reply_text=None,
        ambient_context=ambient_context,
        chat_id=msg.channel_id,
    )

    context = resolved.context

    # Check chat prefs for engine override
    engine_override = resolved.engine_override
    if engine_override is None and chat_prefs:
        pref_engine = await chat_prefs.get_default_engine(msg.channel_id)
        if pref_engine:
            engine_override = pref_engine

    engine = runtime.resolve_engine(
        engine_override=engine_override,
        context=context,
    )

    # -- Resume token (engine-specific lookup) --
    resume_token: ResumeToken | None = None
    if cfg.session_mode == "chat":
        resume_token = await sessions.get(msg.channel_id, engine)

    effective_resume = resolved.resume_token or resume_token

    resolved_runner = runtime.resolve_runner(
        resume_token=effective_resume,
        engine_override=engine,
    )

    if resolved_runner.issue:
        logger.warning(
            "mattermost.runner_unavailable",
            issue=resolved_runner.issue,
            channel_id=msg.channel_id,
        )
        await send(RenderedMessage(text=f"⚠️ {resolved_runner.issue}"))
        return

    context_line = runtime.format_context_line(context)
    try:
        cwd = runtime.resolve_run_cwd(context)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "mattermost.resolve_cwd_error",
            error=str(exc),
            channel_id=msg.channel_id,
        )
        await send(RenderedMessage(text=f"⚠️ {exc}"))
        return
    if cwd:
        bind_run_context(project=context.project if context else None)

    # Thread handling
    if msg.root_id:
        reply_to = MessageRef(
            channel_id=msg.channel_id,
            message_id=msg.post_id,
            thread_id=msg.root_id,
        )
        thread_id = msg.root_id
    else:
        reply_to = None
        thread_id = None

    # -- Persona prompt prepend (@persona_name prefix) --
    final_prompt = resolved.prompt
    if chat_prefs and final_prompt:
        persona_prompt = await _resolve_persona_prefix(final_prompt, chat_prefs)
        if persona_prompt is not None:
            final_prompt = persona_prompt

    # -- Handoff preamble (when resume token is absent) --
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
        message_id=msg.post_id,
        text=final_prompt,
        reply_to=reply_to,
        thread_id=thread_id,
    )

    async def on_thread_known(token: ResumeToken, done: anyio.Event) -> None:
        if cfg.session_mode == "chat":
            await sessions.set(msg.channel_id, token)

    j_run_id = make_run_id(msg.channel_id, msg.post_id) if journal else None

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
            "mattermost.dispatch_error",
            error=str(exc),
            error_type=exc.__class__.__name__,
            channel_id=msg.channel_id,
            post_id=msg.post_id,
        )


async def _dispatch_message(
    msg: MattermostIncomingMessage,
    cfg: MattermostBridgeConfig,
    running_tasks: RunningTasks,
    sessions: ChatSessionStore,
    chat_prefs: ChatPrefsStore | None,
    roundtables: RoundtableStore | None = None,
    journal: Journal | None = None,
    ledger: PendingRunLedger | None = None,
) -> None:
    """Dispatch: slash commands → prompt resolution → engine run."""

    async def send(message: RenderedMessage) -> None:
        await _send_to_channel(cfg, msg.channel_id, message)

    # 1. Command handling
    if await _try_dispatch_command(
        msg,
        cfg,
        running_tasks,
        sessions,
        chat_prefs,
        roundtables,
        send,
        journal=journal,
    ):
        return

    # 2. Prompt resolution (files, voice, trigger, mention strip)
    resolved = await _resolve_prompt(msg, cfg, chat_prefs, send)
    if resolved is None:
        return

    # 3. Engine execution (context, runner, persona, session → run)
    await _run_engine(
        resolved,
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
    cfg: MattermostBridgeConfig,
    *,
    watch_config: bool = False,
    default_engine_override: str | None = None,
    transport_id: str = "mattermost",
    transport_config: object | None = None,
) -> None:
    """Main event loop: connect WebSocket, dispatch messages."""
    await _send_startup(cfg)

    running_tasks: RunningTasks = {}
    sessions = ChatSessionStore(_CONFIG_DIR / "mattermost_sessions.json")
    chat_prefs = ChatPrefsStore(_CONFIG_DIR / "mattermost_prefs.json")
    roundtables = RoundtableStore()
    journal = Journal(_CONFIG_DIR / "journals")
    ledger = PendingRunLedger(_CONFIG_DIR / "pending_runs.json")
    heartbeat_path = _CONFIG_DIR / "heartbeat"

    async def _send_lifecycle_msg(ch_id: str, text: str) -> None:
        await cfg.exec_cfg.transport.send(
            channel_id=ch_id, message=RenderedMessage(text=text)
        )

    await lifecycle.detect_abnormal_termination(
        heartbeat_path=heartbeat_path,
        shutdown_state_path=_SHUTDOWN_STATE_FILE,
        log_prefix="mattermost",
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
    lifecycle.register_sigterm_handler(shutdown, log_prefix="mattermost")

    async with anyio.create_task_group() as dispatch_tg:
        dispatch_tg.start_soon(lifecycle.heartbeat_loop, heartbeat_path)
        async with cfg.bot.websocket_events() as events:
            async for ws_event in events:
                if shutdown.is_set():
                    logger.info("mattermost.shutdown_ws_stop")
                    break

                update = parse_ws_event(
                    ws_event,
                    bot_user_id=cfg.bot_user_id,
                    allowed_channel_ids=cfg.allowed_channel_ids or None,
                    allowed_user_ids=cfg.allowed_user_ids or None,
                )
                if update is None:
                    continue

                if isinstance(update, MattermostReactionEvent):
                    await _handle_cancel_reaction(update, running_tasks, roundtables)
                elif isinstance(update, MattermostIncomingMessage):
                    if not update.text and not update.file_ids:
                        continue
                    logger.info(
                        "mattermost.incoming",
                        channel_id=update.channel_id,
                        sender=update.sender_username,
                        text=update.text[:100],
                        files=len(update.file_ids),
                    )
                    dispatch_tg.start_soon(
                        _dispatch_message,
                        update,
                        cfg,
                        running_tasks,
                        sessions,
                        chat_prefs,
                        roundtables,
                        journal,
                        ledger,
                    )

        await lifecycle.graceful_drain(running_tasks, log_prefix="mattermost")
        lifecycle.save_shutdown_state(
            shutdown_state_path=_SHUTDOWN_STATE_FILE,
            is_sigterm=shutdown.is_set(),
            running_task_count=len(running_tasks),
        )
        lifecycle.cleanup_heartbeat(heartbeat_path)
