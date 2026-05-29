"""Roundtable execution: per-round runners + multi-round orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio

from ...context import RunContext
from ...logging import bind_run_context, get_logger
from ...runner_bridge import IncomingMessage, handle_message
from ...transport import RenderedMessage, SendOptions
from .prompt import _build_round_prompt
from .roles import assign_roles
from .session import RoundtableBridgeCfg, RoundtableSession

if TYPE_CHECKING:
    from ...runner_bridge import RunningTasks

logger = get_logger(__name__)


def _build_role_map(
    session: RoundtableSession, cfg: RoundtableBridgeCfg
) -> dict[str, str | None]:
    """Map each session engine to its configured role (positional, canonicalized).

    Defensive: roles are optional, so a runtime without a roundtable config
    (or without roles) yields an all-``None`` map = no role injection.
    """
    rt = getattr(cfg.runtime, "roundtable", None)
    configured = getattr(rt, "roles", ()) or ()
    roles = assign_roles(list(session.engines), configured)
    return dict(zip(session.engines, roles, strict=False))


async def _run_round_parallel(
    session: RoundtableSession,
    topic: str,
    engines: list[str],
    *,
    cfg: RoundtableBridgeCfg,
    running_tasks: RunningTasks,
    ambient_context: RunContext | None,
    role_map: dict[str, str | None] | None = None,
) -> list[tuple[str, str]]:
    """첫 라운드 엔진들을 병렬 실행하고 결과를 수집."""
    runtime = cfg.runtime
    transport = cfg.exec_cfg.transport
    send_opts = SendOptions(thread_id=session.thread_id)
    results: dict[str, str] = {}

    async def _run_one(engine_id: str) -> None:
        if session.cancel_event.is_set():
            return

        prompt = _build_round_prompt(
            topic,
            session.transcript,
            session.current_round,
            current_round_responses=[],
            role=role_map.get(engine_id) if role_map else None,
        )

        resolved = runtime.resolve_runner(
            resume_token=None,
            engine_override=engine_id,
        )
        if resolved.issue:
            await transport.send(
                channel_id=session.channel_id,
                message=RenderedMessage(text=f"**[{engine_id}]**: {resolved.issue}"),
                options=send_opts,
            )
            return

        context = ambient_context
        context_line = runtime.format_context_line(context)
        try:
            cwd = runtime.resolve_run_cwd(context)
        except Exception as exc:  # noqa: BLE001
            logger.error("roundtable.resolve_cwd_error", error=str(exc))
            await transport.send(
                channel_id=session.channel_id,
                message=RenderedMessage(text=f"{exc}"),
                options=send_opts,
            )
            return

        if cwd:
            bind_run_context(project=context.project if context else None)

        engine_label = f"`{engine_id}`"
        full_context = (
            f"{context_line} | {engine_label}" if context_line else engine_label
        )

        incoming = IncomingMessage(
            channel_id=session.channel_id,
            message_id=session.thread_id,
            text=prompt,
            thread_id=session.thread_id,
        )

        try:
            answer = await handle_message(
                cfg.exec_cfg,
                runner=resolved.runner,
                incoming=incoming,
                resume_token=None,
                context=context,
                context_line=full_context,
                running_tasks=running_tasks,
            )
            if answer:
                results[engine_id] = answer
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "roundtable.agent_error",
                engine=engine_id,
                error=str(exc),
            )
            await transport.send(
                channel_id=session.channel_id,
                message=RenderedMessage(text=f"**[{engine_id}]** error: {exc}"),
                options=send_opts,
            )

    async with anyio.create_task_group() as tg:
        for engine_id in engines:
            tg.start_soon(_run_one, engine_id)

    # 원래 engines 순서 유지
    return [(eid, results[eid]) for eid in engines if eid in results]


async def _run_single_round(
    session: RoundtableSession,
    topic: str,
    engines: list[str],
    *,
    cfg: RoundtableBridgeCfg,
    running_tasks: RunningTasks,
    ambient_context: RunContext | None,
    parallel: bool = False,
    role_map: dict[str, str | None] | None = None,
) -> list[tuple[str, str]]:
    """Run one round of agents and return the round transcript."""
    if parallel and len(engines) > 1:
        return await _run_round_parallel(
            session,
            topic,
            engines,
            cfg=cfg,
            running_tasks=running_tasks,
            ambient_context=ambient_context,
            role_map=role_map,
        )

    runtime = cfg.runtime
    transport = cfg.exec_cfg.transport
    send_opts = SendOptions(thread_id=session.thread_id)
    round_transcript: list[tuple[str, str]] = []

    for engine_id in engines:
        if session.cancel_event.is_set():
            break

        prompt = _build_round_prompt(
            topic,
            session.transcript,
            session.current_round,
            current_round_responses=round_transcript,
            role=role_map.get(engine_id) if role_map else None,
        )

        # Resolve runner
        resolved = runtime.resolve_runner(
            resume_token=None,
            engine_override=engine_id,
        )
        if resolved.issue:
            await transport.send(
                channel_id=session.channel_id,
                message=RenderedMessage(
                    text=f"**[{engine_id}]**: {resolved.issue}",
                ),
                options=send_opts,
            )
            continue

        # Resolve context and cwd
        context = ambient_context
        context_line = runtime.format_context_line(context)
        try:
            cwd = runtime.resolve_run_cwd(context)
        except Exception as exc:  # noqa: BLE001
            logger.error("roundtable.resolve_cwd_error", error=str(exc))
            await transport.send(
                channel_id=session.channel_id,
                message=RenderedMessage(text=f"{exc}"),
                options=send_opts,
            )
            continue

        if cwd:
            bind_run_context(project=context.project if context else None)

        # Engine label in context line
        engine_label = f"`{engine_id}`"
        full_context = (
            f"{context_line} | {engine_label}" if context_line else engine_label
        )

        incoming = IncomingMessage(
            channel_id=session.channel_id,
            message_id=session.thread_id,
            text=prompt,
            thread_id=session.thread_id,
        )

        try:
            answer = await handle_message(
                cfg.exec_cfg,
                runner=resolved.runner,
                incoming=incoming,
                resume_token=None,
                context=context,
                context_line=full_context,
                running_tasks=running_tasks,
            )
            if answer:
                round_transcript.append((engine_id, answer))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "roundtable.agent_error",
                engine=engine_id,
                error=str(exc),
            )
            await transport.send(
                channel_id=session.channel_id,
                message=RenderedMessage(
                    text=f"**[{engine_id}]** error: {exc}",
                ),
                options=send_opts,
            )

    return round_transcript


async def run_roundtable(
    session: RoundtableSession,
    *,
    cfg: RoundtableBridgeCfg,
    chat_prefs: object | None,
    running_tasks: RunningTasks,
    ambient_context: RunContext | None,
    parallel_first_round: bool = False,
) -> None:
    """Run all rounds of a roundtable session."""
    transport = cfg.exec_cfg.transport
    send_opts = SendOptions(thread_id=session.thread_id)
    role_map = _build_role_map(session, cfg)

    for round_num in range(1, session.total_rounds + 1):
        if session.cancel_event.is_set():
            await transport.send(
                channel_id=session.channel_id,
                message=RenderedMessage(text="Roundtable cancelled."),
                options=send_opts,
            )
            break

        session.current_round = round_num

        if session.total_rounds > 1:
            await transport.send(
                channel_id=session.channel_id,
                message=RenderedMessage(
                    text=f"**--- Round {round_num}/{session.total_rounds} ---**",
                ),
                options=send_opts,
            )

        parallel = parallel_first_round and round_num == 1
        round_transcript = await _run_single_round(
            session,
            session.topic,
            session.engines,
            cfg=cfg,
            running_tasks=running_tasks,
            ambient_context=ambient_context,
            parallel=parallel,
            role_map=role_map,
        )
        session.transcript.extend(round_transcript)

    # Completion marker
    if not session.cancel_event.is_set():
        rounds_label = f"{session.current_round}/{session.total_rounds} rounds"
        await transport.send(
            channel_id=session.channel_id,
            message=RenderedMessage(
                text=f"**Roundtable complete** ({rounds_label})",
            ),
            options=send_opts,
        )


async def run_followup_round(
    session: RoundtableSession,
    followup_topic: str,
    engines_filter: list[str] | None,
    *,
    cfg: RoundtableBridgeCfg,
    running_tasks: RunningTasks,
    ambient_context: RunContext | None,
) -> None:
    """Run a follow-up round on a completed roundtable session."""
    transport = cfg.exec_cfg.transport
    send_opts = SendOptions(thread_id=session.thread_id)
    engines = engines_filter or session.engines

    session.completed = False
    session.current_round += 1

    engines_display = ", ".join(f"`{e}`" for e in engines)
    await transport.send(
        channel_id=session.channel_id,
        message=RenderedMessage(
            text=f"**--- Follow-up Round {session.current_round} ({engines_display}) ---**",
        ),
        options=send_opts,
    )

    round_transcript = await _run_single_round(
        session,
        followup_topic,
        engines,
        cfg=cfg,
        running_tasks=running_tasks,
        ambient_context=ambient_context,
        role_map=_build_role_map(session, cfg),
    )
    session.transcript.extend(round_transcript)

    session.completed = True

    await transport.send(
        channel_id=session.channel_id,
        message=RenderedMessage(
            text=f"**Follow-up complete** (Round {session.current_round})",
        ),
        options=send_opts,
    )
