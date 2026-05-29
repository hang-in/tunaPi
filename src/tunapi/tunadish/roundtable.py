"""tunadish roundtable wiring.

The roundtable *engine* lives in ``core.roundtable`` and is transport-agnostic.
This module only assembles the tunadish-specific pieces (transport, presenter,
conversation→thread mapping, background execution) and delegates to the shared
``dispatch_roundtable_command`` seam.

Key difference from Slack/Mattermost: tunadish conversations are linear and have
no thread concept, so the roundtable ``thread_id`` is mapped 1:1 to the
``conversation_id``.  ``!rt follow`` / ``!rt close`` therefore resolve the
session by conversation id.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..core.chat_loop_helpers import (
    archive_roundtable_thread,
    dispatch_roundtable_command,
)
from ..core.roundtable import (
    RoundtableSession,
    RoundtableStore,
    handle_rt as core_handle_rt,
    run_followup_round,
    run_roundtable,
)
from ..logging import get_logger
from ..runner_bridge import ExecBridgeConfig
from ..transport import RenderedMessage
from ..transport_runtime import TransportRuntime

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TunadishRoundtableCfg:
    """Satisfies the ``RoundtableBridgeCfg`` protocol (runtime + exec_cfg)."""

    runtime: TransportRuntime
    exec_cfg: ExecBridgeConfig


def render_roundtable_header(topic: str, rounds: int, engines: list[str]) -> str:
    engines_display = ", ".join(f"`{e}`" for e in engines)
    rounds_display = f"{rounds} round{'s' if rounds > 1 else ''}"
    return (
        "**Roundtable**\n\n"
        f"**Topic:** {topic}\n"
        f"**Engines:** {engines_display} | **Rounds:** {rounds_display}\n\n"
        "---"
    )


async def dispatch_rt(
    args: str,
    *,
    conv_id: str,
    runtime: TransportRuntime,
    transport: Any,
    presenter: Any,
    roundtables: RoundtableStore,
    running_tasks: Any,
    context_store: Any | None,
    facade: Any | None = None,
    journal: Any | None = None,
    task_group: Any | None = None,
) -> None:
    """Wire and dispatch ``!rt`` for a tunadish conversation.

    ``task_group`` — when provided, long-running start/follow rounds are spawned
    on it so the WebSocket receive loop is not blocked.  When ``None`` (tests),
    rounds run inline for determinism.
    """
    exec_cfg = ExecBridgeConfig(
        transport=transport,
        presenter=presenter,
        final_notify=False,
    )
    cfg = TunadishRoundtableCfg(runtime=runtime, exec_cfg=exec_cfg)

    async def rt_send(message: RenderedMessage) -> None:
        # Roundtable usage/error/close text renders inline as a chat message,
        # consistent with the agent outputs (also message.new).
        await transport.send(channel_id=conv_id, message=message)

    async def _maybe_spawn(run: Callable[[], Awaitable[None]]) -> None:
        if task_group is not None:
            task_group.start_soon(run)
        else:
            await run()

    async def _start_rt(topic: str, rounds: int, engines: list[str]) -> None:
        ref = await transport.send(
            channel_id=conv_id,
            message=RenderedMessage(
                text=render_roundtable_header(topic, rounds, engines)
            ),
        )
        if ref is None:
            logger.error("roundtable.header_send_failed", conv_id=conv_id)
            return

        # thread_id == conv_id: lets `!rt follow` / `!rt close` find the session.
        session = RoundtableSession(
            thread_id=conv_id,
            channel_id=conv_id,
            topic=topic,
            engines=engines,
            total_rounds=rounds,
        )
        roundtables.put(session)

        ambient = await context_store.get_context(conv_id) if context_store else None

        async def _run() -> None:
            try:
                await run_roundtable(
                    session,
                    cfg=cfg,
                    chat_prefs=None,
                    running_tasks=running_tasks,
                    ambient_context=ambient,
                    parallel_first_round=runtime.roundtable.parallel_first_round,
                )
            except Exception:  # noqa: BLE001
                logger.exception("roundtable.run_failed", conv_id=conv_id)
            finally:
                roundtables.complete(conv_id)

        await _maybe_spawn(_run)

    async def _continue_rt(
        session: RoundtableSession,
        topic: str,
        engines_filter: list[str] | None,
        ambient_context: Any | None,
    ) -> None:
        async def _run() -> None:
            try:
                await run_followup_round(
                    session,
                    topic,
                    engines_filter,
                    cfg=cfg,
                    running_tasks=running_tasks,
                    ambient_context=ambient_context,
                )
            except Exception:  # noqa: BLE001
                logger.exception("roundtable.followup_failed", conv_id=conv_id)

        await _maybe_spawn(_run)

    async def _archive_rt(
        session: RoundtableSession,
        project: str | None,
        branch: str | None,
    ) -> None:
        await archive_roundtable_thread(
            session,
            journal,
            rt_send,
            close_message="Roundtable closed.",
            facade=facade,
            project=project,
            branch=branch,
        )

    await dispatch_roundtable_command(
        args,
        runtime=runtime,
        channel_id=conv_id,
        thread_id=conv_id,
        chat_prefs=context_store,
        roundtables=roundtables,
        send=rt_send,
        start_roundtable=_start_rt,
        handle_rt_command=core_handle_rt,
        continue_roundtable_session=_continue_rt,
        archive_roundtable_session=_archive_rt,
    )
