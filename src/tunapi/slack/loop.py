"""Main Socket Mode event loop for the Slack transport."""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import anyio

from ..core import lifecycle
from ..core.memory_facade import ProjectMemoryFacade
from ..core.roundtable import (
    RoundtableSession,
    RoundtableStore,
    run_followup_round,
    run_roundtable,
)
from ..core.voice import is_audio_file, transcribe_audio
from ..journal import (
    Journal,
    JournalEntry,
    PendingRunLedger,
    build_handoff_preamble,
    make_run_id,
)
from ..logging import bind_run_context, get_logger
from ..model import ResumeToken
from ..runner_bridge import IncomingMessage, handle_message
from ..runners.run_options import EngineRunOptions, apply_run_options
from ..transport import MessageRef, RenderedMessage
from ..utils.paths import reset_run_base_dir, set_run_base_dir
from .bridge import CANCEL_EMOJI, SlackBridgeConfig
from .chat_prefs import ChatPrefsStore
from ..core.project_sessions import ProjectSessionStore
from .chat_sessions import ChatSessionStore
from ..core.commands import parse_command
from .commands import (
    handle_branch,
    handle_cancel,
    handle_context,
    handle_help,
    handle_memory,
    handle_model,
    handle_models,
    handle_persona,
    handle_project,
    handle_review,
    handle_rt,
    handle_status,
    handle_trigger,
)
from .files import handle_file_get, handle_file_put
from .parsing import SlackMessageEvent, SlackReactionEvent, parse_envelope
from .trigger_mode import resolve_trigger_mode, should_trigger, strip_mention

if TYPE_CHECKING:
    from ..runner_bridge import RunningTasks

logger = get_logger(__name__)

# Callback type for sending a message to a channel.
type _SendFn = Callable[[RenderedMessage], Awaitable[None]]

_CONFIG_DIR = Path.home() / ".tunapi"
_SHUTDOWN_STATE_FILE = _CONFIG_DIR / "slack_last_shutdown.json"

_PERSONA_PREFIX_RE = re.compile(r"^@(\w+)\s+", re.UNICODE)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def _resolve_upload_dir(cfg: SlackBridgeConfig, channel_id: str) -> Path:
    """Resolve the upload target directory for a channel."""
    from ..core.files import resolve_incoming_dir

    context = cfg.runtime.default_context_for_chat(channel_id)
    project = context.project if context else None
    return resolve_incoming_dir(project or "default")


async def _put_files(
    cfg: SlackBridgeConfig,
    channel_id: str,
    files: list[dict[str, Any]],
) -> list:
    """Download Slack files and save to project directory."""
    target_dir = _resolve_upload_dir(cfg, channel_id)
    return await handle_file_put(
        bot_token=cfg.bot._client._bot_token,
        files=files,
        target_dir=target_dir,
        deny_globs=cfg.files_deny_globs,
        max_bytes=cfg.files_max_upload_bytes,
    )


# ---------------------------------------------------------------------------
# Voice helpers
# ---------------------------------------------------------------------------


async def _handle_voice(
    msg: SlackMessageEvent,
    cfg: SlackBridgeConfig,
) -> str | None:
    """If the message has an audio attachment, transcribe it and return text."""
    if not cfg.voice_enabled or not msg.files:
        return None

    for file_info in msg.files:
        mime = file_info.get("mimetype", "")
        if not is_audio_file(mime):
            continue
        size = file_info.get("size", 0)
        if size > cfg.voice_max_bytes:
            logger.warning("voice.too_large", size=size, max=cfg.voice_max_bytes)
            continue

        url = file_info.get("url_private_download")
        if not url:
            continue

        from .files import _download_slack_file

        audio_data = await _download_slack_file(
            url,
            cfg.bot._client._bot_token,
            max_bytes=cfg.voice_max_bytes,
        )
        if audio_data is None:
            continue

        filename = file_info.get("name", "audio.ogg")
        text = await transcribe_audio(
            audio_data,
            filename,
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


# ---------------------------------------------------------------------------
# File command handler
# ---------------------------------------------------------------------------


async def _handle_file_command(
    args: str,
    msg: SlackMessageEvent,
    cfg: SlackBridgeConfig,
    send: _SendFn,
) -> bool:
    """Handle /file put or /file get. Returns True if handled."""
    if not cfg.files_enabled:
        await send(RenderedMessage(text="File transfer is disabled."))
        return True

    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""
    subargs = parts[1] if len(parts) > 1 else ""

    if subcmd == "put":
        if not msg.files:
            await send(RenderedMessage(text="Attach files to the message to upload."))
            return True

        results = await _put_files(cfg, msg.channel_id, msg.files)
        text = (
            "\n".join(f"- {r.message}" for r in results)
            if results
            else "No files processed."
        )
        await send(RenderedMessage(text=text))
        return True

    elif subcmd == "get":
        rel_path = subargs.strip()
        if not rel_path:
            await send(RenderedMessage(text="Usage: `!file get <path>`"))
            return True

        context = cfg.runtime.default_context_for_chat(msg.channel_id)
        root = cfg.runtime.resolve_run_cwd(context) or Path.cwd()

        filename, error, content = await handle_file_get(
            rel_path=rel_path,
            root=root,
            deny_globs=cfg.files_deny_globs,
            max_bytes=cfg.files_max_download_bytes,
        )
        if error or filename is None or content is None:
            await send(RenderedMessage(text=error or "Failed to read file."))
            return True

        # Upload file back to Slack
        file_id = await cfg.bot.upload_file(
            filename,
            content,
            channel_id=msg.channel_id,
            thread_ts=msg.thread_ts or msg.ts,
        )
        if not file_id:
            await send(RenderedMessage(text="Failed to upload file."))
        return True

    else:
        await send(
            RenderedMessage(
                text="Usage: `!file put` (with attachments) or `!file get <path>`"
            )
        )
        return True


# ---------------------------------------------------------------------------
# Roundtable helpers
# ---------------------------------------------------------------------------


async def _start_roundtable(
    channel_id: str,
    topic: str,
    rounds: int,
    engines: list[str],
    *,
    cfg: SlackBridgeConfig,
    running_tasks: RunningTasks,
    chat_prefs: ChatPrefsStore | None,
    roundtables: RoundtableStore,
) -> None:
    """Create a roundtable thread and run all rounds."""
    engines_display = ", ".join(f"`{e}`" for e in engines)
    rounds_display = f"{rounds} round{'s' if rounds > 1 else ''}"
    header = (
        f"*Roundtable*\n\n"
        f"*Topic:* {topic}\n"
        f"*Engines:* {engines_display} | *Rounds:* {rounds_display}\n\n"
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


async def _archive_roundtable(
    session: RoundtableSession,
    journal: Journal | None,
    send: _SendFn,
    *,
    facade: ProjectMemoryFacade | None = None,
    project: str | None = None,
    branch: str | None = None,
) -> None:
    """Archive roundtable transcript to journal, then notify."""
    if journal and session.transcript:
        import time as _t

        ts = _t.strftime("%Y-%m-%dT%H:%M:%S")
        run_id = f"rt:{session.thread_id}"
        transcript_lines = []
        for engine, answer in session.transcript:
            transcript_lines.append(f"[{engine}]: {answer[:500]}")
        entry = JournalEntry(
            run_id=run_id,
            channel_id=session.channel_id,
            timestamp=ts,
            event="roundtable_closed",
            data={
                "topic": session.topic,
                "engines": session.engines,
                "rounds": session.current_round,
                "transcript": "\n\n".join(transcript_lines),
            },
        )
        with contextlib.suppress(Exception):
            await journal.append(entry)

    # Project memory — best-effort
    if facade and project and session.transcript:
        with contextlib.suppress(Exception):
            await facade.save_roundtable(
                session, project, branch_name=branch, auto_synthesis=True, auto_structured=True
            )

    await send(RenderedMessage(text="Roundtable closed."))


async def _dispatch_rt_command(
    args: str,
    msg: SlackMessageEvent,
    cfg: SlackBridgeConfig,
    running_tasks: RunningTasks,
    chat_prefs: ChatPrefsStore | None,
    roundtables: RoundtableStore | None,
    send: _SendFn,
    journal: Journal | None = None,
    facade: ProjectMemoryFacade | None = None,
) -> None:
    """Handle the !rt / /rt command, including follow-up and close."""
    continue_rt = None
    close_rt = None

    # Resolve project/branch for project-memory archive
    _ambient_ctx = (
        await chat_prefs.get_context(msg.channel_id) if chat_prefs else None
    )
    _pm_project = _ambient_ctx.project if _ambient_ctx else None
    _pm_branch = _ambient_ctx.branch if _ambient_ctx else None

    thread_id = msg.thread_ts

    if thread_id and roundtables:
        # Check for completed session (follow-up / close)
        completed_session = roundtables.get_completed(thread_id)
        if completed_session:
            async def continue_rt(
                topic: str,
                engines_filter: list[str] | None,
                *,
                _s: Any = completed_session,
                _ctx: Any = _ambient_ctx,
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
                await _archive_roundtable(
                    _s, journal, send,
                    facade=facade, project=_pm_project, branch=_pm_branch,
                )
                _rt.remove(_tid)

        # Also allow close on active (non-completed) sessions
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
                    await _archive_roundtable(
                        session, journal, send,
                        facade=facade, project=_pm_project, branch=_pm_branch,
                    )
                _rt.remove(_tid)

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
        close_roundtable=close_rt,
        thread_id=thread_id,
    )


# ---------------------------------------------------------------------------
# Persona prefix
# ---------------------------------------------------------------------------


async def _resolve_persona_prefix(
    prompt: str, chat_prefs: ChatPrefsStore
) -> str | None:
    """If prompt starts with @persona_name, prepend the persona prompt."""
    m = _PERSONA_PREFIX_RE.match(prompt)
    if not m:
        return None
    name = m.group(1).lower()
    persona = await chat_prefs.get_persona(name)
    if persona is None:
        return None
    user_text = prompt[m.end() :]
    return f"[역할: {persona.name}]\n{persona.prompt}\n\n---\n\n{user_text}"


# ---------------------------------------------------------------------------
# Auto-bind channel → project
# ---------------------------------------------------------------------------


async def _auto_bind_channel_project(
    channel_id: str,
    cfg: SlackBridgeConfig,
) -> None:
    """Auto-bind a channel to a discovered project if channel name matches."""
    runtime = cfg.runtime
    if runtime.projects_root is None:
        return
    if runtime._projects.project_for_chat(channel_id) is not None:
        return

    channel = await cfg.bot.get_channel(channel_id)
    if channel is None or not channel.name:
        return

    root = Path(runtime.projects_root).expanduser()
    if not root.is_dir():
        return

    # Case-insensitive match against subdirectories
    channel_lower = channel.name.lower()
    for candidate in root.iterdir():
        if candidate.is_dir() and candidate.name.lower() == channel_lower:
            runtime._projects.register_discovered(
                alias=candidate.name,
                path=candidate,
                chat_id=channel_id,
            )
            logger.info(
                "slack.auto_bind_project",
                channel_id=channel_id,
                channel_name=channel.name,
                project=candidate.name,
            )
            return


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Cancel reaction handler
# ---------------------------------------------------------------------------


async def _handle_cancel_reaction(
    reaction: SlackReactionEvent,
    running_tasks: RunningTasks,
    roundtables: RoundtableStore | None = None,
) -> None:
    if reaction.emoji != CANCEL_EMOJI:
        return

    # Cancel roundtable session if reaction on header post
    if roundtables:
        session = roundtables.get(reaction.item_ts)
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
        if str(ref.message_id) == reaction.item_ts:
            logger.info(
                "slack.cancel_by_reaction",
                ts=reaction.item_ts,
                user_id=reaction.user_id,
            )
            task.cancel_requested.set()
            return


# ---------------------------------------------------------------------------
# Prompt resolution
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ResolvedPrompt:
    """Result of prompt resolution before engine dispatch."""

    text: str
    file_context: str  # empty string if no files


async def _resolve_prompt(
    msg: SlackMessageEvent,
    cfg: SlackBridgeConfig,
    chat_prefs: ChatPrefsStore | None,
    send: _SendFn,
) -> _ResolvedPrompt | None:
    """Resolve user input into a clean prompt text.

    Handles auto file upload, file+text attachment, voice transcription,
    trigger mode check, and @mention stripping.
    Returns None if the message should not be dispatched to an engine.
    """
    # -- Auto file put: attachment with no text → save to project --
    if msg.files and not msg.text.strip() and cfg.files_enabled:
        results = await _put_files(cfg, msg.channel_id, msg.files)
        text = (
            "\n".join(f"- {r.message}" for r in results)
            if results
            else "No files processed."
        )
        await send(RenderedMessage(text=text))
        return None

    # -- File + text: save files, add absolute paths to prompt --
    file_context = ""
    if msg.files and msg.text.strip() and cfg.files_enabled:
        results = await _put_files(cfg, msg.channel_id, msg.files)
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
    trigger_mode = await resolve_trigger_mode(msg.channel_id, chat_prefs)
    if not should_trigger(msg, bot_user_id=cfg.bot_user_id, trigger_mode=trigger_mode):
        return None

    # Strip @mention from text
    prompt_text = strip_mention(prompt_text, cfg.bot_user_id)
    if not prompt_text:
        return None

    return _ResolvedPrompt(text=prompt_text, file_context=file_context)


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------


async def _try_dispatch_command(
    msg: SlackMessageEvent,
    cfg: SlackBridgeConfig,
    running_tasks: RunningTasks,
    sessions: ChatSessionStore,
    chat_prefs: ChatPrefsStore | None,
    roundtables: RoundtableStore | None,
    send: _SendFn,
    journal: Journal | None = None,
    facade: ProjectMemoryFacade | None = None,
    project_sessions: ProjectSessionStore | None = None,
) -> bool:
    """Handle slash/bang commands. Returns True if a command was dispatched."""
    cmd, args = parse_command(msg.text)
    if cmd is None:
        return False

    runtime = cfg.runtime

    match cmd:
        case "new":
            # Clear unified project session if available
            if project_sessions and chat_prefs:
                ctx = await chat_prefs.get_context(msg.channel_id)
                if ctx and ctx.project:
                    await project_sessions.clear(ctx.project)
            # Also clear legacy per-transport session
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
        case "models":
            await handle_models(
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
                config_path=runtime.config_path,
                send=send,
            )
        case "persona":
            await handle_persona(
                args,
                chat_prefs=chat_prefs,
                send=send,
            )
        case "memory":
            _ctx = await chat_prefs.get_context(msg.channel_id) if chat_prefs else None
            _engine = (await chat_prefs.get_default_engine(msg.channel_id)) if chat_prefs else None
            await handle_memory(
                args,
                project=_ctx.project if _ctx else None,
                facade=facade,
                current_engine=_engine or runtime.default_engine,
                send=send,
            )
        case "branch":
            _ctx = await chat_prefs.get_context(msg.channel_id) if chat_prefs else None
            await handle_branch(
                args,
                project=_ctx.project if _ctx else None,
                facade=facade,
                send=send,
            )
        case "review":
            _ctx = await chat_prefs.get_context(msg.channel_id) if chat_prefs else None
            await handle_review(
                args,
                project=_ctx.project if _ctx else None,
                facade=facade,
                send=send,
            )
        case "context":
            _ctx = await chat_prefs.get_context(msg.channel_id) if chat_prefs else None
            await handle_context(
                project=_ctx.project if _ctx else None,
                facade=facade,
                send=send,
            )
        case "rt":
            await _dispatch_rt_command(
                args,
                msg,
                cfg,
                running_tasks,
                chat_prefs,
                roundtables,
                send,
                journal=journal,
                facade=facade,
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
            await _handle_file_command(args, msg, cfg, send)
        case _:
            return False

    return True


# ---------------------------------------------------------------------------
# Engine execution
# ---------------------------------------------------------------------------


async def _run_engine(
    resolved_prompt: _ResolvedPrompt,
    msg: SlackMessageEvent,
    cfg: SlackBridgeConfig,
    running_tasks: RunningTasks,
    sessions: ChatSessionStore,
    chat_prefs: ChatPrefsStore | None,
    send: _SendFn,
    journal: Journal | None = None,
    ledger: PendingRunLedger | None = None,
    project_sessions: ProjectSessionStore | None = None,
) -> None:
    runtime = cfg.runtime

    # -- Resolve engine/context --
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
    engine_override = resolved.engine_override
    if engine_override is None and chat_prefs:
        pref_engine = await chat_prefs.get_default_engine(msg.channel_id)
        if pref_engine:
            engine_override = pref_engine

    engine = runtime.resolve_engine(
        engine_override=engine_override,
        context=context,
    )

    context_line = runtime.format_context_line(context)
    try:
        cwd = runtime.resolve_run_cwd(context)
    except Exception as exc:  # noqa: BLE001
        await send(RenderedMessage(text=f"{exc}"))
        return

    # -- Session handling --
    resume_token: ResumeToken | None = None
    if cfg.session_mode == "chat":
        resume_token = await sessions.get(msg.channel_id, engine, cwd=cwd)

    effective_resume = resolved.resume_token or resume_token

    resolved_runner = runtime.resolve_runner(
        resume_token=effective_resume,
        engine_override=engine,
    )

    if resolved_runner.issue:
        logger.warning("slack.runner_unavailable", issue=resolved_runner.issue)
        await send(RenderedMessage(text=f"{resolved_runner.issue}"))
        return

    if cwd:
        bind_run_context(project=context.project if context else None)

    # Thread handling
    reply_to = MessageRef(
        channel_id=msg.channel_id,
        message_id=msg.ts,
        thread_id=msg.thread_ts or msg.ts,
    )

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
            if not j_entries and (context is None or context.project is None):
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
            await sessions.set(msg.channel_id, token, cwd=cwd)

    j_run_id = make_run_id(msg.channel_id, msg.ts) if journal else None

    # -- Per-engine model override --
    model_override = None
    if chat_prefs:
        model_override = await chat_prefs.get_engine_model(msg.channel_id, engine)
    run_options = EngineRunOptions(model=model_override) if model_override else None

    run_base_token = set_run_base_dir(cwd)
    try:
        with apply_run_options(run_options):
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
                project_sessions=project_sessions,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "slack.dispatch_error",
            error=str(exc),
            error_type=exc.__class__.__name__,
            channel_id=msg.channel_id,
        )
    finally:
        reset_run_base_dir(run_base_token)


# ---------------------------------------------------------------------------
# Message dispatch
# ---------------------------------------------------------------------------


async def _dispatch_message(
    msg: SlackMessageEvent,
    cfg: SlackBridgeConfig,
    running_tasks: RunningTasks,
    sessions: ChatSessionStore,
    chat_prefs: ChatPrefsStore | None,
    roundtables: RoundtableStore | None = None,
    journal: Journal | None = None,
    ledger: PendingRunLedger | None = None,
    facade: ProjectMemoryFacade | None = None,
    project_sessions: ProjectSessionStore | None = None,
) -> None:
    """Dispatch: slash commands → prompt resolution → engine run."""

    # Auto-bind channel to project by name match (lazy, one-time)
    await _auto_bind_channel_project(msg.channel_id, cfg)

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
        facade=facade,
        project_sessions=project_sessions,
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
        project_sessions=project_sessions,
    )


# ---------------------------------------------------------------------------
# Main event loop
# ---------------------------------------------------------------------------


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
    project_sessions = ProjectSessionStore(_CONFIG_DIR / "sessions.json")
    chat_prefs = ChatPrefsStore(_CONFIG_DIR / "slack_prefs.json")
    roundtables = RoundtableStore(_CONFIG_DIR / "slack_roundtables.json")
    journal = Journal(_CONFIG_DIR / "journals")
    ledger = PendingRunLedger(_CONFIG_DIR / "slack_pending_runs.json")
    facade = ProjectMemoryFacade()
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
                    await _handle_cancel_reaction(update, running_tasks, roundtables)
                elif isinstance(update, SlackMessageEvent):
                    if not update.text and not update.files:
                        continue
                    logger.info(
                        "slack.incoming",
                        channel_id=update.channel_id,
                        user_id=update.user_id,
                        text=update.text[:100],
                        files=len(update.files) if update.files else 0,
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
                        facade,
                        project_sessions,
                    )

        await lifecycle.graceful_drain(running_tasks, log_prefix="slack")
        lifecycle.save_shutdown_state(
            shutdown_state_path=_SHUTDOWN_STATE_FILE,
            is_sigterm=shutdown.is_set(),
            running_task_count=len(running_tasks),
        )
        lifecycle.cleanup_heartbeat(heartbeat_path)
