"""Main WebSocket event loop for the Mattermost transport."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio

from ..logging import bind_run_context, get_logger
from ..model import ResumeToken
from ..runner_bridge import IncomingMessage, handle_message
from ..transport import MessageRef, RenderedMessage, SendOptions
from .bridge import CANCEL_EMOJI, MattermostBridgeConfig
from .parsing import parse_ws_event
from .types import MattermostIncomingMessage, MattermostReactionEvent

if TYPE_CHECKING:
    from ..runner_bridge import RunningTasks

logger = get_logger(__name__)


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


async def _handle_cancel_reaction(
    reaction: MattermostReactionEvent,
    running_tasks: RunningTasks,
) -> None:
    """Cancel a running task when the user reacts with 🛑."""
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


async def _dispatch_message(
    msg: MattermostIncomingMessage,
    cfg: MattermostBridgeConfig,
    running_tasks: RunningTasks,
    sessions: ChatSessionStore,
) -> None:
    """Resolve engine/runner and dispatch to handle_message."""
    runtime = cfg.runtime

    # Handle /new command — reset session
    if msg.text.strip().lower() == "/new":
        sessions.clear(msg.channel_id)
        await cfg.exec_cfg.transport.send(
            channel_id=msg.channel_id,
            message=RenderedMessage(text="새 대화를 시작합니다."),
        )
        return

    # In chat mode, use stored resume token to continue conversation
    resume_token: ResumeToken | None = None
    if cfg.session_mode == "chat":
        resume_token = sessions.get(msg.channel_id)

    # Resolve engine, context, prompt from message text
    resolved = runtime.resolve_message(
        text=msg.text,
        reply_text=None,
        ambient_context=None,
        chat_id=msg.channel_id,
    )

    # Use resolved resume token if found in text, otherwise use session
    effective_resume = resolved.resume_token or resume_token

    # Resolve engine: directive > project default > global default
    context = resolved.context
    engine = runtime.resolve_engine(
        engine_override=resolved.engine_override,
        context=context,
    )

    # If resume token is for a different engine, discard it
    if effective_resume is not None and effective_resume.engine != engine:
        logger.debug(
            "mattermost.resume_engine_mismatch",
            resume_engine=effective_resume.engine,
            target_engine=engine,
        )
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
        await cfg.exec_cfg.transport.send(
            channel_id=msg.channel_id,
            message=RenderedMessage(text=f"⚠️ {resolved_runner.issue}"),
        )
        return

    context_line = runtime.format_context_line(context)
    cwd = runtime.resolve_run_cwd(context)

    if cwd:
        bind_run_context(project=context.project if context else None)

    # If the user sent in a thread, reply in the same thread.
    # Otherwise reply directly in the channel (no thread).
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

    # Callback to store resume token when session starts
    async def on_thread_known(token: ResumeToken, done: anyio.Event) -> None:
        if cfg.session_mode == "chat":
            sessions.set(msg.channel_id, token)
            logger.debug(
                "mattermost.session_stored",
                channel_id=msg.channel_id,
                resume=token.value,
            )

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
                    if not update.text:
                        continue
                    logger.info(
                        "mattermost.incoming",
                        channel_id=update.channel_id,
                        sender=update.sender_username,
                        text=update.text[:100],
                    )
                    tg.start_soon(
                        _dispatch_message, update, cfg, running_tasks, sessions
                    )
