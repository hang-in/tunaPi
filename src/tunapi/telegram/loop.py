"""Main long-polling event loop for the Telegram transport.

Dispatch logic lives in :mod:`loop_dispatch`; this module handles
initialization, store setup, config watch, and the poll loop.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

import anyio

from ..config import ConfigError
from ..config_watch import ConfigReload, watch_config as watch_config_changes
from ..commands import list_command_ids
from ..logging import get_logger
from ..model import ResumeToken
from ..scheduler import ThreadJob, ThreadScheduler
from ..settings import TelegramTransportSettings
from ..transport import MessageRef
from ..context import RunContext
from ..ids import RESERVED_CHAT_COMMANDS
from .bridge import TelegramBridgeConfig, send_plain
from .commands.handlers import (
    get_reserved_commands,
    set_command_menu,
)
from .client import poll_incoming
from ..core.roundtable import RoundtableStore
from .chat_prefs import ChatPrefsStore, resolve_prefs_path
from .chat_sessions import ChatSessionStore, resolve_sessions_path
from .forward_coalescing import (
    ForwardCoalescer,
    is_forwarded as _is_forwarded,  # noqa: F401 — re-exported for tests
)
from .commands.handlers import handle_file_put_default  # noqa: F401
from .voice import transcribe_voice  # noqa: F401 — re-exported for test monkeypatch
from .loop_dispatch import (
    _dispatch_pending_prompt,
    _send_queued_progress,
    route_update,
    run_job,
    run_prompt_from_upload,
    resolve_prompt_message,
)
from .loop_state import (
    TelegramLoopContext,
    TelegramLoopState,
    allowed_chat_ids as _allowed_chat_ids,
    diff_keys as _diff_keys,
)
from .media_group_buffer import MediaGroupBuffer
from .resume_resolver import ResumeResolver
from .topic_state import TopicStateStore, resolve_state_path
from .topics import (
    _resolve_topics_scope,
    _validate_topics_setup,
)
from .types import (
    TelegramIncomingUpdate,
)

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

__all__ = ["poll_updates", "run_main_loop", "send_with_resume"]

_handle_file_put_default = handle_file_put_default  # re-exported for test monkeypatch


# ---------------------------------------------------------------------------
# Startup helpers (module-level, no closure needed)
# ---------------------------------------------------------------------------


async def _send_startup(cfg: TelegramBridgeConfig) -> None:
    from ..markdown import MarkdownParts
    from ..transport import RenderedMessage
    from .render import prepare_telegram

    logger.debug("startup.message", text=cfg.startup_msg)
    parts = MarkdownParts(header=cfg.startup_msg)
    text, entities = prepare_telegram(parts)
    message = RenderedMessage(text=text, extra={"entities": entities})
    sent = await cfg.exec_cfg.transport.send(
        channel_id=cfg.chat_id,
        message=message,
    )
    if sent is not None:
        logger.info("startup.sent", chat_id=cfg.chat_id)


async def _drain_backlog(cfg: TelegramBridgeConfig, offset: int | None) -> int | None:
    drained = 0
    while True:
        updates = await cfg.bot.get_updates(
            offset=offset,
            timeout_s=0,
            allowed_updates=["message", "callback_query"],
        )
        if updates is None:
            logger.info("startup.backlog.failed")
            return offset
        logger.debug("startup.backlog.updates", updates=updates)
        if not updates:
            if drained:
                logger.info("startup.backlog.drained", count=drained)
            return offset
        offset = updates[-1].update_id + 1
        drained += len(updates)


async def poll_updates(
    cfg: TelegramBridgeConfig,
    *,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
) -> AsyncIterator[TelegramIncomingUpdate]:
    offset: int | None = None
    offset = await _drain_backlog(cfg, offset)
    await _send_startup(cfg)

    async for msg in poll_incoming(
        cfg.bot,
        chat_ids=lambda: _allowed_chat_ids(cfg),
        offset=offset,
        sleep=sleep,
    ):
        yield msg


# ---------------------------------------------------------------------------
# Resume helper (module-level)
# ---------------------------------------------------------------------------


async def _wait_for_resume(running_task: object) -> ResumeToken | None:
    if running_task.resume is not None:  # type: ignore[union-attr]
        return running_task.resume  # type: ignore[union-attr]
    resume: ResumeToken | None = None

    async with anyio.create_task_group() as tg:

        async def wait_resume() -> None:
            nonlocal resume
            await running_task.resume_ready.wait()  # type: ignore[union-attr]
            resume = running_task.resume  # type: ignore[union-attr]
            tg.cancel_scope.cancel()

        async def wait_done() -> None:
            await running_task.done.wait()  # type: ignore[union-attr]
            tg.cancel_scope.cancel()

        tg.start_soon(wait_resume)
        tg.start_soon(wait_done)

    return resume


async def send_with_resume(
    cfg: TelegramBridgeConfig,
    enqueue: Callable[
        [
            int,
            int,
            str,
            ResumeToken,
            RunContext | None,
            int | None,
            tuple[int, int | None] | None,
            MessageRef | None,
        ],
        Awaitable[None],
    ],
    running_task: object,
    chat_id: int,
    user_msg_id: int,
    thread_id: int | None,
    session_key: tuple[int, int | None] | None,
    text: str,
) -> None:
    reply = partial(
        send_plain,
        cfg.exec_cfg.transport,
        chat_id=chat_id,
        user_msg_id=user_msg_id,
        thread_id=thread_id,
    )
    resume = await _wait_for_resume(running_task)
    if resume is None:
        await reply(
            text="resume token not ready yet; try replying to the final message.",
            notify=False,
        )
        return
    progress_ref = await _send_queued_progress(
        cfg,
        chat_id=chat_id,
        user_msg_id=user_msg_id,
        thread_id=thread_id,
        resume_token=resume,
        context=running_task.context,  # type: ignore[union-attr]
    )
    await enqueue(
        chat_id,
        user_msg_id,
        text,
        resume,
        running_task.context,  # type: ignore[union-attr]
        thread_id,
        session_key,
        progress_ref,
    )


# ---------------------------------------------------------------------------
# Main event loop
# ---------------------------------------------------------------------------


async def run_main_loop(
    cfg: TelegramBridgeConfig,
    poller: Callable[
        [TelegramBridgeConfig], AsyncIterator[TelegramIncomingUpdate]
    ] = poll_updates,
    *,
    watch_config: bool | None = None,
    default_engine_override: str | None = None,
    transport_id: str | None = None,
    transport_config: TelegramTransportSettings | None = None,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
) -> None:
    state = TelegramLoopState(
        running_tasks={},
        pending_prompts={},
        media_groups={},
        command_ids={
            command_id.lower()
            for command_id in list_command_ids(allowlist=cfg.runtime.allowlist)
        },
        reserved_commands=get_reserved_commands(cfg.runtime),
        reserved_chat_commands=set(RESERVED_CHAT_COMMANDS),
        transport_snapshot=(
            transport_config.model_dump() if transport_config is not None else None
        ),
        topic_store=None,
        chat_session_store=None,
        chat_prefs=None,
        resolved_topics_scope=None,
        topics_chat_ids=frozenset(),
        bot_username=None,
        forward_coalesce_s=max(0.0, float(cfg.forward_coalesce_s)),
        media_group_debounce_s=max(0.0, float(cfg.media_group_debounce_s)),
        transport_id=transport_id,
        roundtable_store=None,
        seen_update_ids=set(),
        seen_update_order=deque(),
        seen_message_keys=set(),
        seen_messages_order=deque(),
    )

    def refresh_topics_scope() -> None:
        if cfg.topics.enabled:
            (
                state.resolved_topics_scope,
                state.topics_chat_ids,
            ) = _resolve_topics_scope(cfg)
        else:
            state.resolved_topics_scope = None
            state.topics_chat_ids = frozenset()

    def refresh_commands() -> None:
        allowlist = cfg.runtime.allowlist
        state.command_ids = {
            command_id.lower() for command_id in list_command_ids(allowlist=allowlist)
        }
        state.reserved_commands = get_reserved_commands(cfg.runtime)

    try:
        config_path = cfg.runtime.config_path
        if config_path is not None:
            state.chat_prefs = ChatPrefsStore(resolve_prefs_path(config_path))
            logger.info(
                "chat_prefs.enabled",
                state_path=str(resolve_prefs_path(config_path)),
            )
            rt_path = config_path.parent / "telegram_roundtables.json"
            state.roundtable_store = RoundtableStore(rt_path)
            logger.info("roundtable_store.enabled", state_path=str(rt_path))
        if cfg.session_mode == "chat":
            if config_path is None:
                raise ConfigError(
                    "session_mode=chat but config path is not set; cannot locate state file."
                )
            state.chat_session_store = ChatSessionStore(
                resolve_sessions_path(config_path)
            )
            cleared = await state.chat_session_store.sync_startup_cwd(Path.cwd())
            if cleared:
                logger.info(
                    "chat_sessions.cleared",
                    reason="startup_cwd_changed",
                    cwd=str(Path.cwd()),
                    state_path=str(resolve_sessions_path(config_path)),
                )
            logger.info(
                "chat_sessions.enabled",
                state_path=str(resolve_sessions_path(config_path)),
            )
        if cfg.topics.enabled:
            if config_path is None:
                raise ConfigError(
                    "topics enabled but config path is not set; cannot locate state file."
                )
            state.topic_store = TopicStateStore(resolve_state_path(config_path))
            await _validate_topics_setup(cfg)
            refresh_topics_scope()
            logger.info(
                "topics.enabled",
                scope=cfg.topics.scope,
                resolved_scope=state.resolved_topics_scope,
                state_path=str(resolve_state_path(config_path)),
            )
        await set_command_menu(cfg)
        try:
            me = await cfg.bot.get_me()
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "trigger_mode.bot_username.failed",
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            me = None
        if me is not None and me.username:
            state.bot_username = me.username.lower()
        else:
            logger.info("trigger_mode.bot_username.unavailable")

        async with anyio.create_task_group() as tg:
            poller_fn: Callable[
                [TelegramBridgeConfig], AsyncIterator[TelegramIncomingUpdate]
            ]
            if poller is poll_updates:
                poller_fn = cast(
                    Callable[
                        [TelegramBridgeConfig], AsyncIterator[TelegramIncomingUpdate]
                    ],
                    partial(poll_updates, sleep=sleep),
                )
            else:
                poller_fn = poller
            config_path = cfg.runtime.config_path
            watch_enabled = bool(watch_config) and config_path is not None

            async def handle_reload(reload: ConfigReload) -> None:
                refresh_commands()
                refresh_topics_scope()
                await set_command_menu(cfg)
                if state.transport_snapshot is not None:
                    new_snapshot = reload.settings.transports.telegram.model_dump()
                    changed = _diff_keys(state.transport_snapshot, new_snapshot)
                    if changed:
                        logger.warning(
                            "config.reload.transport_config_changed",
                            transport="telegram",
                            keys=changed,
                            restart_required=True,
                        )
                        state.transport_snapshot = new_snapshot
                if (
                    state.transport_id is not None
                    and reload.settings.transport != state.transport_id
                ):
                    logger.warning(
                        "config.reload.transport_changed",
                        old=state.transport_id,
                        new=reload.settings.transport,
                        restart_required=True,
                    )
                    state.transport_id = reload.settings.transport

            if watch_enabled and config_path is not None:

                async def run_config_watch() -> None:
                    await watch_config_changes(
                        config_path=config_path,
                        runtime=cfg.runtime,
                        default_engine_override=default_engine_override,
                        on_reload=handle_reload,
                    )

                tg.start_soon(run_config_watch)

            # -- Build thread job runner using context --
            # (run_thread_job still needs a thin closure for the scheduler callback)
            async def _run_thread_job(job: ThreadJob) -> None:
                await run_job(
                    ctx,
                    cast(int, job.chat_id),
                    cast(int, job.user_msg_id),
                    job.text,
                    job.resume_token,
                    job.context,
                    cast(int | None, job.thread_id),
                    job.session_key,
                    None,
                    scheduler.note_thread_known,
                    None,
                    job.progress_ref,
                )

            scheduler = ThreadScheduler(task_group=tg, run_job=_run_thread_job)

            resume_resolver = ResumeResolver(
                cfg=cfg,
                task_group=tg,
                running_tasks=state.running_tasks,
                enqueue_resume=scheduler.enqueue_resume,
                topic_store=state.topic_store,
                chat_session_store=state.chat_session_store,
            )

            # -- Assemble context (before coalescer/buffer that need ctx) --
            ctx = TelegramLoopContext(
                cfg=cfg,
                state=state,
                tg=tg,
                scheduler=scheduler,
                forward_coalescer=None,  # type: ignore[arg-type] — set below
                media_group_buffer=None,  # type: ignore[arg-type] — set below
                resume_resolver=resume_resolver,
            )

            forward_coalescer = ForwardCoalescer(
                task_group=tg,
                debounce_s=state.forward_coalesce_s,
                sleep=sleep,
                dispatch=partial(_dispatch_pending_prompt, ctx),
                pending=state.pending_prompts,
            )

            media_group_buffer = MediaGroupBuffer(
                task_group=tg,
                debounce_s=state.media_group_debounce_s,
                sleep=sleep,
                cfg=cfg,
                chat_prefs=state.chat_prefs,
                topic_store=state.topic_store,
                bot_username=state.bot_username,
                command_ids=lambda: state.command_ids,
                reserved_chat_commands=state.reserved_chat_commands,
                groups=state.media_groups,
                run_prompt_from_upload=partial(run_prompt_from_upload, ctx),
                resolve_prompt_message=partial(resolve_prompt_message, ctx),
            )

            ctx.forward_coalescer = forward_coalescer
            ctx.media_group_buffer = media_group_buffer

            allowed_user_ids_set = set(cfg.allowed_user_ids)

            async for update in poller_fn(cfg):
                await route_update(ctx, update, allowed_user_ids_set)
    finally:
        await cfg.exec_cfg.transport.close()
