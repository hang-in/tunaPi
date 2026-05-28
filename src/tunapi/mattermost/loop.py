"""Main WebSocket event loop for the Mattermost transport."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import anyio

from ..core import lifecycle
from ..core.chat_loop_helpers import (
    _PERSONA_PREFIX_RE,  # noqa: F401 - compatibility for existing tests/imports
    archive_roundtable_thread,
    auto_bind_channel_project,
    dispatch_roundtable_command,
    handle_cancel_reaction_by_message_id,
    handle_file_command,
    handle_voice_attachments,
    resolve_chat_prompt,
    resolve_persona_prefix,
    ResolvedPrompt as _ResolvedPrompt,
    resolve_upload_dir,
    run_chat_engine,
    send_to_channel,
    start_roundtable_thread,
)
from ..core.memory_facade import ProjectMemoryFacade
from ..journal import (
    Journal,
    PendingRunLedger,
)
from ..logging import get_logger
from ..runner_bridge import handle_message
from ..runners.run_options import apply_run_options
from ..transport import MessageRef, RenderedMessage
from ..utils.paths import reset_run_base_dir, set_run_base_dir
from .bridge import CANCEL_EMOJI, MattermostBridgeConfig
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
from .roundtable import (
    RoundtableSession,
    RoundtableStore,
    run_followup_round,
)
from .files import handle_file_get, handle_file_put
from .parsing import parse_ws_event
from .trigger_mode import resolve_trigger_mode, should_trigger, strip_mention
from .types import MattermostIncomingMessage, MattermostReactionEvent

if TYPE_CHECKING:
    from ..runner_bridge import RunningTasks

logger = get_logger(__name__)

# Callback type for sending a message to a channel.
type _SendFn = Callable[[RenderedMessage], Awaitable[None]]

_CONFIG_DIR = Path.home() / ".tunapi"
_SHUTDOWN_STATE_FILE = _CONFIG_DIR / "last_shutdown.json"


def _resolve_upload_dir(cfg: MattermostBridgeConfig, channel_id: str) -> Path:
    return resolve_upload_dir(cfg.runtime, channel_id)


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


async def _attach_referenced_files(
    cfg: MattermostBridgeConfig,
    channel_id: str,
    answer: str,
) -> None:
    """Detect local file paths in the answer and upload them to MM."""
    from ..core.files import extract_file_paths

    paths = extract_file_paths(answer)
    if not paths:
        return
    for p in paths:
        try:
            data = p.read_bytes()
            file_info = await cfg.bot.upload_file(channel_id, p.name, data)
            if file_info:
                await cfg.bot.send_message(
                    channel_id,
                    f"`{p}`",
                    file_ids=[file_info.id],
                )
                logger.info("file.auto_attach", path=str(p), channel_id=channel_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("file.auto_attach_failed", path=str(p), error=str(exc))


async def _send_startup(cfg: MattermostBridgeConfig) -> None:
    msg = RenderedMessage(text=cfg.startup_msg)
    await cfg.exec_cfg.transport.send(channel_id=cfg.channel_id, message=msg)
    logger.info("mattermost.startup_sent")


async def _send_to_channel(
    cfg: MattermostBridgeConfig,
    channel_id: str,
    message: RenderedMessage,
) -> None:
    await send_to_channel(cfg, channel_id, message)


async def _handle_cancel_reaction(
    reaction: MattermostReactionEvent,
    running_tasks: RunningTasks,
    roundtables: RoundtableStore | None = None,
) -> None:
    await handle_cancel_reaction_by_message_id(
        emoji=reaction.emoji_name,
        cancel_emoji=CANCEL_EMOJI,
        message_id=reaction.post_id,
        user_id=reaction.user_id,
        running_tasks=running_tasks,
        roundtables=roundtables,
        transport_log_event="mattermost.cancel_by_reaction",
        message_id_log_key="post_id",
    )


async def _handle_voice(
    msg: MattermostIncomingMessage,
    cfg: MattermostBridgeConfig,
) -> str | None:
    """If the message has an audio attachment, transcribe it and return text."""
    if not cfg.voice_enabled or not msg.file_ids:
        return None

    async def _get_file_info(file_id: str) -> Any | None:
        info = await cfg.bot._client.get_file_info(file_id)
        return (file_id, info) if info is not None else None

    attachments = [
        attachment
        for attachment in [await _get_file_info(file_id) for file_id in msg.file_ids]
        if attachment is not None
    ]
    return await handle_voice_attachments(
        attachments,
        channel_id=msg.channel_id,
        voice_max_bytes=cfg.voice_max_bytes,
        voice_model=cfg.voice_model,
        voice_base_url=cfg.voice_base_url,
        voice_api_key=cfg.voice_api_key,
        get_mime_type=lambda attachment: attachment[1].mime_type,
        get_size=lambda attachment: attachment[1].size,
        get_filename=lambda attachment: attachment[1].name,
        get_audio_data=lambda attachment: cfg.bot.get_file(attachment[0]),
    )


async def _handle_file_command(
    args: str,
    msg: MattermostIncomingMessage,
    cfg: MattermostBridgeConfig,
) -> bool:
    """Handle /file put or /file get. Returns True if handled."""

    async def _get_file(
        rel_path: str, root: Path
    ) -> tuple[str | None, str | None, Any | None]:
        filename, error, content = await handle_file_get(
            client=cfg.bot,
            channel_id=msg.channel_id,
            rel_path=rel_path,
            root=root,
            deny_globs=cfg.files_deny_globs,
            max_bytes=cfg.files_max_download_bytes,
        )
        return filename, error, content

    async def _upload_file(filename: str, content: Any, rel_path: str) -> bool:
        file_info = await cfg.bot.upload_file(msg.channel_id, filename, content)
        if file_info:
            await cfg.bot.send_message(
                msg.channel_id,
                f"`{rel_path}`",
                file_ids=[file_info.id],
            )
            return True
        return False

    return await handle_file_command(
        args,
        files_enabled=cfg.files_enabled,
        channel_id=msg.channel_id,
        runtime=cfg.runtime,
        send=lambda message: _send_to_channel(cfg, msg.channel_id, message),
        has_attachments=lambda: bool(msg.file_ids),
        put_files=lambda: _put_files(cfg, msg.channel_id, list(msg.file_ids)),
        get_file=_get_file,
        upload_file=_upload_file,
        put_usage="Attach files to the message to upload.",
        get_usage="Usage: `/file get <path>`",
        unknown_usage="Usage: `/file put` (with attachments) or `/file get <path>`",
    )


async def _resolve_persona_prefix(
    prompt: str, chat_prefs: ChatPrefsStore
) -> str | None:
    return await resolve_persona_prefix(prompt, chat_prefs)


def _render_roundtable_header(topic: str, rounds: int, engines: list[str]) -> str:
    engines_display = ", ".join(f"`{e}`" for e in engines)
    rounds_display = f"{rounds} round{'s' if rounds > 1 else ''}"
    return (
        f"**🔵 Roundtable**\n\n"
        f"**Topic:** {topic}\n"
        f"**Engines:** {engines_display} | **Rounds:** {rounds_display}\n\n"
        f"---"
    )


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
    await start_roundtable_thread(
        channel_id,
        topic,
        rounds,
        engines,
        cfg=cfg,
        running_tasks=running_tasks,
        chat_prefs=chat_prefs,
        roundtables=roundtables,
        render_header=_render_roundtable_header,
    )


async def _archive_roundtable(
    session: RoundtableSession,
    journal: Journal | None,
    send: _SendFn,
    *,
    facade: ProjectMemoryFacade | None = None,
    project: str | None = None,
    branch: str | None = None,
) -> None:
    await archive_roundtable_thread(
        session,
        journal,
        send,
        close_message="🔴 라운드테이블이 종료되었습니다.",
        facade=facade,
        project=project,
        branch=branch,
    )


async def _dispatch_rt_command(
    args: str,
    msg: MattermostIncomingMessage,
    cfg: MattermostBridgeConfig,
    running_tasks: RunningTasks,
    chat_prefs: ChatPrefsStore | None,
    roundtables: RoundtableStore | None,
    send: _SendFn,
    journal: Journal | None = None,
    facade: ProjectMemoryFacade | None = None,
) -> None:
    """Handle the !rt / /rt command, including follow-up and close."""

    async def _continue_roundtable_session(
        session: RoundtableSession,
        topic: str,
        engines_filter: list[str] | None,
        ambient_context: Any | None,
    ) -> None:
        await run_followup_round(
            session,
            topic,
            engines_filter,
            cfg=cfg,
            running_tasks=running_tasks,
            ambient_context=ambient_context,
        )

    async def _archive_roundtable_session(
        session: RoundtableSession,
        project: str | None,
        branch: str | None,
    ) -> None:
        await _archive_roundtable(
            session,
            journal,
            send,
            facade=facade,
            project=project,
            branch=branch,
        )

    await dispatch_roundtable_command(
        args,
        runtime=cfg.runtime,
        channel_id=msg.channel_id,
        thread_id=msg.root_id,
        chat_prefs=chat_prefs,
        roundtables=roundtables,
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
        handle_rt_command=handle_rt,
        continue_roundtable_session=_continue_roundtable_session,
        archive_roundtable_session=_archive_roundtable_session,
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
            _engine = (
                (await chat_prefs.get_default_engine(msg.channel_id))
                if chat_prefs
                else None
            )
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
    return await resolve_chat_prompt(
        text=msg.text,
        channel_id=msg.channel_id,
        chat_prefs=chat_prefs,
        files_enabled=cfg.files_enabled,
        has_attachments=lambda: bool(msg.file_ids),
        put_files=lambda: _put_files(cfg, msg.channel_id, list(msg.file_ids)),
        handle_voice=lambda: _handle_voice(msg, cfg),
        send=send,
        resolve_trigger=resolve_trigger_mode,
        should_trigger_prompt=lambda trigger_mode: should_trigger(
            msg,
            bot_username=cfg.bot_username,
            trigger_mode=trigger_mode,
        ),
        strip_mention_from_prompt=lambda prompt_text: strip_mention(
            prompt_text,
            cfg.bot_username,
        ),
    )


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
    project_sessions: ProjectSessionStore | None = None,
) -> None:
    """Resolve engine/context and run the agent.

    Error boundary policy:
    - Runner unavailable (resolve_runner.issue): warn user via message, return
    - CWD resolution failure: warn user via message, return
    - handle_message() failure: log only (no user message)
    - Command handler errors: propagate (crash = bug in our code)
    """
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

    await run_chat_engine(
        resolved_prompt,
        channel_id=msg.channel_id,
        message_id=msg.post_id,
        runtime=cfg.runtime,
        exec_cfg=cfg.exec_cfg,
        session_mode=cfg.session_mode,
        running_tasks=running_tasks,
        sessions=sessions,
        chat_prefs=chat_prefs,
        send=send,
        reply_to=reply_to,
        thread_id=thread_id,
        handle_message_func=handle_message,
        resolve_persona_prefix_func=_resolve_persona_prefix,
        set_run_base_dir_func=set_run_base_dir,
        reset_run_base_dir_func=reset_run_base_dir,
        apply_run_options_func=apply_run_options,
        logger_obj=logger,
        resolve_cwd_log_event="mattermost.resolve_cwd_error",
        resolve_cwd_log_extra={},
        resolve_cwd_message=lambda exc: f"⚠️ {exc}",
        runner_unavailable_log_event="mattermost.runner_unavailable",
        runner_unavailable_log_extra={},
        runner_unavailable_message=lambda issue: f"⚠️ {issue}",
        dispatch_error_log_event="mattermost.dispatch_error",
        dispatch_error_log_extra={"post_id": msg.post_id},
        journal=journal,
        ledger=ledger,
        project_sessions=project_sessions,
        after_answer=lambda answer: _attach_referenced_files(
            cfg,
            msg.channel_id,
            answer,
        ),
    )


async def _auto_bind_channel_project(
    channel_id: str,
    cfg: MattermostBridgeConfig,
) -> None:
    """Auto-bind a channel to a discovered project if channel name matches a projects_root subdirectory."""

    async def _get_channel_name(channel_id: str) -> str | None:
        channel = await cfg.bot._client.get_channel(channel_id)
        return channel.name if channel else None

    await auto_bind_channel_project(
        channel_id,
        cfg.runtime,
        get_channel_name=_get_channel_name,
        log_event="mattermost.auto_bind_project",
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


async def run_main_loop(
    cfg: MattermostBridgeConfig,
    *,
    watch_config: bool = False,
    default_engine_override: str | None = None,
    transport_id: str = "mattermost",
    transport_config: object | None = None,
) -> None:
    """Main event loop: connect WebSocket, dispatch messages."""
    from ..core.files import cleanup_incoming

    await _send_startup(cfg)
    cleanup_incoming()

    running_tasks: RunningTasks = {}
    sessions = ChatSessionStore(_CONFIG_DIR / "mattermost_sessions.json")
    project_sessions = ProjectSessionStore(_CONFIG_DIR / "sessions.json")
    chat_prefs = ChatPrefsStore(_CONFIG_DIR / "mattermost_prefs.json")
    roundtables = RoundtableStore(_CONFIG_DIR / "mattermost_roundtables.json")
    journal = Journal(_CONFIG_DIR / "journals")
    ledger = PendingRunLedger(_CONFIG_DIR / "pending_runs.json")
    facade = ProjectMemoryFacade()
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
                        facade,
                        project_sessions,
                    )

        await lifecycle.graceful_drain(running_tasks, log_prefix="mattermost")
        lifecycle.save_shutdown_state(
            shutdown_state_path=_SHUTDOWN_STATE_FILE,
            is_sigterm=shutdown.is_set(),
            running_task_count=len(running_tasks),
        )
        lifecycle.cleanup_heartbeat(heartbeat_path)
