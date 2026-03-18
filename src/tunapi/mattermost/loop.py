"""Main WebSocket event loop for the Mattermost transport."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from ..logging import bind_run_context, get_logger
from ..model import ResumeToken
from ..runner_bridge import IncomingMessage, handle_message
from ..transport import MessageRef, RenderedMessage, SendOptions
from .bridge import CANCEL_EMOJI, MattermostBridgeConfig
from .chat_prefs import ChatPrefsStore
from .commands import (
    handle_cancel,
    handle_help,
    handle_model,
    handle_status,
    handle_trigger,
    parse_slash_command,
)
from .files import handle_file_get, handle_file_put
from .parsing import parse_ws_event
from .trigger_mode import resolve_trigger_mode, should_trigger, strip_mention
from .types import MattermostIncomingMessage, MattermostReactionEvent
from .voice import is_audio_file, transcribe_audio

if TYPE_CHECKING:
    from ..runner_bridge import RunningTasks

logger = get_logger(__name__)

_CONFIG_DIR = Path.home() / ".tunapi"


@dataclass
class ChatSessionStore:
    """Stores the last resume token per channel for session_mode='chat'."""

    _sessions: dict[str, ResumeToken] = field(default_factory=dict)

    def get(self, channel_id: str) -> ResumeToken | None:
        return self._sessions.get(channel_id)

    def set(self, channel_id: str, token: ResumeToken) -> None:
        self._sessions[channel_id] = token

    def clear(self, channel_id: str) -> None:
        self._sessions.pop(channel_id, None)


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
) -> None:
    if reaction.emoji_name != CANCEL_EMOJI:
        return
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
            logger.info("voice.transcribed", channel_id=msg.channel_id, length=len(text))
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
            cfg, msg.channel_id,
            RenderedMessage(text="File transfer is disabled."),
        )
        return True

    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""
    subargs = parts[1] if len(parts) > 1 else ""

    # Resolve working directory
    context = cfg.runtime.default_context_for_chat(msg.channel_id)
    cwd = cfg.runtime.resolve_run_cwd(context)
    root = cwd or Path.cwd()

    if subcmd == "put":
        if not msg.file_ids:
            await _send_to_channel(
                cfg, msg.channel_id,
                RenderedMessage(text="Attach files to the message to upload."),
            )
            return True

        target_dir = root / cfg.files_uploads_dir
        results = await handle_file_put(
            client=cfg.bot,
            channel_id=msg.channel_id,
            file_ids=list(msg.file_ids),
            target_dir=target_dir,
            deny_globs=cfg.files_deny_globs,
            max_bytes=cfg.files_max_upload_bytes,
        )
        text = "\n".join(f"- {r}" for r in results) if results else "No files processed."
        await _send_to_channel(cfg, msg.channel_id, RenderedMessage(text=text))
        return True

    elif subcmd == "get":
        rel_path = subargs.strip()
        if not rel_path:
            await _send_to_channel(
                cfg, msg.channel_id,
                RenderedMessage(text="Usage: `/file get <path>`"),
            )
            return True

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
                cfg, msg.channel_id,
                RenderedMessage(text="Failed to upload file."),
            )
        return True

    else:
        await _send_to_channel(
            cfg, msg.channel_id,
            RenderedMessage(text="Usage: `/file put` (with attachments) or `/file get <path>`"),
        )
        return True


async def _dispatch_message(
    msg: MattermostIncomingMessage,
    cfg: MattermostBridgeConfig,
    running_tasks: RunningTasks,
    sessions: ChatSessionStore,
    chat_prefs: ChatPrefsStore | None,
) -> None:
    """Dispatch: slash commands → voice → trigger check → engine."""
    runtime = cfg.runtime

    # Helper to send a message to the channel
    async def send(message: RenderedMessage) -> None:
        await _send_to_channel(cfg, msg.channel_id, message)

    # -- Slash commands --
    cmd, args = parse_slash_command(msg.text)
    if cmd is not None:
        match cmd:
            case "new":
                sessions.clear(msg.channel_id)
                await send(RenderedMessage(text="새 대화를 시작합니다."))
                return
            case "help":
                await handle_help(runtime=runtime, send=send)
                return
            case "model":
                await handle_model(
                    args, channel_id=msg.channel_id,
                    runtime=runtime, chat_prefs=chat_prefs, send=send,
                )
                return
            case "trigger":
                await handle_trigger(
                    args, channel_id=msg.channel_id,
                    chat_prefs=chat_prefs, send=send,
                )
                return
            case "status":
                has_session = sessions.get(msg.channel_id) is not None
                await handle_status(
                    channel_id=msg.channel_id, runtime=runtime,
                    chat_prefs=chat_prefs,
                    session_engine=None, has_session=has_session, send=send,
                )
                return
            case "cancel":
                await handle_cancel(
                    channel_id=msg.channel_id,
                    running_tasks=running_tasks, send=send,
                )
                return
            case "file":
                await _handle_file_command(args, msg, cfg)
                return

    # -- Auto file put: attachment with no text → save to project --
    if msg.file_ids and not msg.text.strip() and cfg.files_enabled:
        context = cfg.runtime.default_context_for_chat(msg.channel_id)
        cwd = cfg.runtime.resolve_run_cwd(context)
        root = cwd or Path.cwd()
        target_dir = root / cfg.files_uploads_dir
        results = await handle_file_put(
            client=cfg.bot,
            channel_id=msg.channel_id,
            file_ids=list(msg.file_ids),
            target_dir=target_dir,
            deny_globs=cfg.files_deny_globs,
            max_bytes=cfg.files_max_upload_bytes,
        )
        text = "\n".join(f"- {r}" for r in results) if results else "No files processed."
        await send(RenderedMessage(text=text))
        return

    # -- File + text: save files, add paths to prompt --
    file_context = ""
    if msg.file_ids and msg.text.strip() and cfg.files_enabled:
        context = cfg.runtime.default_context_for_chat(msg.channel_id)
        cwd = cfg.runtime.resolve_run_cwd(context)
        root = cwd or Path.cwd()
        target_dir = root / cfg.files_uploads_dir
        results = await handle_file_put(
            client=cfg.bot,
            channel_id=msg.channel_id,
            file_ids=list(msg.file_ids),
            target_dir=target_dir,
            deny_globs=cfg.files_deny_globs,
            max_bytes=cfg.files_max_upload_bytes,
        )
        saved = [r for r in results if r.startswith("saved")]
        if saved:
            file_context = "\n[Attached files: " + ", ".join(saved) + "]\n"

    # -- Voice transcription --
    voice_text = await _handle_voice(msg, cfg)
    prompt_text = voice_text or msg.text
    if file_context:
        prompt_text = f"{prompt_text}\n{file_context}"
    if not prompt_text:
        return

    # -- Trigger mode check --
    trigger_mode = await resolve_trigger_mode(
        msg.channel_id, chat_prefs,
    )
    if not should_trigger(msg, bot_username=cfg.bot_username, trigger_mode=trigger_mode):
        return
    # Strip @mention from text
    prompt_text = strip_mention(prompt_text, cfg.bot_username)
    if not prompt_text:
        return

    # -- Resume token --
    resume_token: ResumeToken | None = None
    if cfg.session_mode == "chat":
        resume_token = sessions.get(msg.channel_id)

    # -- Resolve engine/context --
    resolved = runtime.resolve_message(
        text=prompt_text,
        reply_text=None,
        ambient_context=None,
        chat_id=msg.channel_id,
    )

    effective_resume = resolved.resume_token or resume_token
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

    if effective_resume is not None and effective_resume.engine != engine:
        effective_resume = None

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
    cwd = runtime.resolve_run_cwd(context)
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

    incoming = IncomingMessage(
        channel_id=msg.channel_id,
        message_id=msg.post_id,
        text=resolved.prompt,
        reply_to=reply_to,
        thread_id=thread_id,
    )

    async def on_thread_known(token: ResumeToken, done: anyio.Event) -> None:
        if cfg.session_mode == "chat":
            sessions.set(msg.channel_id, token)

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
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "mattermost.dispatch_error",
            error=str(exc),
            error_type=exc.__class__.__name__,
            channel_id=msg.channel_id,
            post_id=msg.post_id,
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
    sessions = ChatSessionStore()
    chat_prefs = ChatPrefsStore(_CONFIG_DIR / "mattermost_prefs.json")

    async with cfg.bot.websocket_events() as events:
        async with anyio.create_task_group() as tg:
            async for ws_event in events:
                update = parse_ws_event(
                    ws_event,
                    bot_user_id=cfg.bot_user_id,
                    allowed_channel_ids=cfg.allowed_channel_ids or None,
                    allowed_user_ids=cfg.allowed_user_ids or None,
                )
                if update is None:
                    continue

                if isinstance(update, MattermostReactionEvent):
                    await _handle_cancel_reaction(update, running_tasks)
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
                    tg.start_soon(
                        _dispatch_message, update, cfg, running_tasks,
                        sessions, chat_prefs,
                    )
