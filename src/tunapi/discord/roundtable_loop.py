from __future__ import annotations

from typing import TYPE_CHECKING

from tunapi.core.roundtable import (
    RoundtableSession,
    RoundtableStore,
    run_followup_round,
    run_roundtable,
)
from tunapi.logging import get_logger
from tunapi.transport import RenderedMessage, SendOptions

if TYPE_CHECKING:
    from tunapi.context import RunContext
    from tunapi.runner_bridge import RunningTasks

    from .bridge import DiscordBridgeConfig

logger = get_logger(__name__)


async def _start_roundtable(
    channel_id: int,
    topic: str,
    rounds: int,
    engines: list[str],
    *,
    cfg: DiscordBridgeConfig,
    running_tasks: RunningTasks,
    roundtables: RoundtableStore,
    run_context: RunContext | None,
    run_roundtable_fn=run_roundtable,
) -> None:
    """Create a roundtable thread and run all rounds."""
    engines_display = ", ".join(f"`{e}`" for e in engines)
    rounds_display = f"{rounds} round{'s' if rounds > 1 else ''}"
    header = (
        f"**Roundtable**\n\n"
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

    logger.info(
        "roundtable.start",
        thread_id=thread_id,
        topic=topic,
        engines=engines,
        rounds=rounds,
    )

    try:
        await run_roundtable_fn(
            session,
            cfg=cfg,
            chat_prefs=None,
            running_tasks=running_tasks,
            ambient_context=run_context,
            parallel_first_round=cfg.runtime.roundtable.parallel_first_round,
        )
    finally:
        roundtables.complete(thread_id)


async def _archive_roundtable(
    session: RoundtableSession,
    cfg: DiscordBridgeConfig,
) -> None:
    """Archive roundtable transcript, then notify."""
    send_opts = SendOptions(thread_id=session.thread_id)
    await cfg.exec_cfg.transport.send(
        channel_id=session.channel_id,
        message=RenderedMessage(text="Roundtable closed."),
        options=send_opts,
    )


async def _dispatch_rt_command(
    args: str,
    *,
    channel_id: int,
    thread_id: int | None,
    cfg: DiscordBridgeConfig,
    running_tasks: RunningTasks,
    roundtables: RoundtableStore,
    run_context: RunContext | None,
    send_opts: SendOptions,
    start_roundtable_fn=None,
    archive_roundtable_fn=_archive_roundtable,
    run_followup_round_fn=run_followup_round,
) -> None:
    """Handle the !rt command, including follow-up and close."""
    from tunapi.slack.commands import handle_rt

    continue_rt = None
    close_rt = None

    rt_thread_id = str(thread_id) if thread_id else None

    if rt_thread_id and roundtables:
        completed_session = roundtables.get_completed(rt_thread_id)
        if completed_session:

            async def continue_rt(
                topic: str,
                engines_filter: list[str] | None,
                *,
                _s: RoundtableSession = completed_session,
                _ctx: RunContext | None = run_context,
            ) -> None:
                await run_followup_round_fn(
                    _s,
                    topic,
                    engines_filter,
                    cfg=cfg,
                    running_tasks=running_tasks,
                    ambient_context=_ctx,
                )

            async def close_rt(
                *,
                _tid: str = rt_thread_id,
                _rt: RoundtableStore = roundtables,
                _s: RoundtableSession = completed_session,
            ) -> None:
                await archive_roundtable_fn(_s, cfg)
                _rt.remove(_tid)

        active_session = roundtables.get(rt_thread_id)
        if active_session and not active_session.completed and close_rt is None:

            async def close_rt(
                *,
                _tid: str = rt_thread_id,
                _rt: RoundtableStore = roundtables,
            ) -> None:
                session = _rt.get(_tid)
                if session:
                    session.cancel_event.set()
                    await archive_roundtable_fn(session, cfg)
                _rt.remove(_tid)

    async def send_fn(msg: RenderedMessage) -> None:
        await cfg.exec_cfg.transport.send(
            channel_id=channel_id,
            message=msg,
            options=send_opts,
        )

    await handle_rt(
        args,
        runtime=cfg.runtime,
        send=send_fn,
        start_roundtable=lambda topic, rounds, engines: (
            start_roundtable_fn or _start_roundtable
        )(
            channel_id,
            topic,
            rounds,
            engines,
            cfg=cfg,
            running_tasks=running_tasks,
            roundtables=roundtables,
            run_context=run_context,
        ),
        continue_roundtable=continue_rt,
        close_roundtable=close_rt,
        thread_id=rt_thread_id,
    )
