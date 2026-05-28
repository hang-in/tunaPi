from __future__ import annotations

import logging
from typing import Any

from tunapi.core.roundtable import (
    RoundtableSession,
    RoundtableStore,
)
from tunapi.transport import SendOptions
from .bridge import DiscordBridgeConfig
from . import roundtable_loop as _roundtable_loop

logger = get_logger = lambda name: logging.getLogger(name)


async def _start_roundtable(
    channel_id: int,
    topic: str,
    rounds: int,
    engines: list[str],
    *,
    cfg: DiscordBridgeConfig,
    running_tasks: Any,
    roundtables: RoundtableStore,
    run_context: Any,
) -> None:
    import tunapi.discord.loop as loop

    await _roundtable_loop._start_roundtable(
        channel_id,
        topic,
        rounds,
        engines,
        cfg=cfg,
        running_tasks=running_tasks,
        roundtables=roundtables,
        run_context=run_context,
        run_roundtable_fn=loop.run_roundtable,
    )


async def _archive_roundtable(
    session: RoundtableSession,
    cfg: DiscordBridgeConfig,
) -> None:
    await _roundtable_loop._archive_roundtable(session, cfg)


async def _dispatch_rt_command(
    args: str,
    *,
    channel_id: int,
    thread_id: int | None,
    cfg: DiscordBridgeConfig,
    running_tasks: Any,
    roundtables: RoundtableStore,
    run_context: Any,
    send_opts: SendOptions,
) -> None:
    import tunapi.discord.loop as loop

    await _roundtable_loop._dispatch_rt_command(
        args,
        channel_id=channel_id,
        thread_id=thread_id,
        cfg=cfg,
        running_tasks=running_tasks,
        roundtables=roundtables,
        run_context=run_context,
        send_opts=send_opts,
        start_roundtable_fn=loop._start_roundtable,
        archive_roundtable_fn=loop._archive_roundtable,
        run_followup_round_fn=loop.run_followup_round,
    )
