"""Roundtable: sequential multi-agent opinion collection in a thread.

Agents within the same round can reference earlier agents' responses.
After completion, users can continue the discussion with ``!rt follow``.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio

from ..context import RunContext
from ..logging import bind_run_context, get_logger
from ..runner_bridge import IncomingMessage, handle_message
from ..transport import RenderedMessage, SendOptions
from ..transport_runtime import RoundtableConfig

if TYPE_CHECKING:
    from ..runner_bridge import RunningTasks
    from .bridge import MattermostBridgeConfig
    from .chat_prefs import ChatPrefsStore

logger = get_logger(__name__)

# Completed sessions are kept for follow-up discussions.
_SESSION_TTL_SECONDS = 3600  # 1 hour

# Maximum length of an agent answer included in context prompts.
_MAX_ANSWER_LENGTH = 4000


@dataclass(slots=True)
class RoundtableSession:
    thread_id: str
    channel_id: str
    topic: str
    engines: list[str]
    total_rounds: int
    current_round: int = 0
    transcript: list[tuple[str, str]] = field(default_factory=list)
    cancel_event: anyio.Event = field(default_factory=anyio.Event)
    completed: bool = False


class RoundtableStore:
    """In-memory store mapping thread_id -> active/completed roundtable session."""

    def __init__(self) -> None:
        self._sessions: dict[str, RoundtableSession] = {}
        self._completed_at: dict[str, float] = {}

    def get(self, thread_id: str) -> RoundtableSession | None:
        self._evict_expired()
        return self._sessions.get(thread_id)

    def get_completed(self, thread_id: str) -> RoundtableSession | None:
        self._evict_expired()
        s = self._sessions.get(thread_id)
        return s if s and s.completed else None

    def put(self, session: RoundtableSession) -> None:
        self._sessions[session.thread_id] = session
        self._completed_at.pop(session.thread_id, None)

    def remove(self, thread_id: str) -> RoundtableSession | None:
        self._completed_at.pop(thread_id, None)
        return self._sessions.pop(thread_id, None)

    def complete(self, thread_id: str) -> None:
        session = self._sessions.get(thread_id)
        if session:
            session.completed = True
            self._completed_at[thread_id] = time.monotonic()

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [
            tid
            for tid, ts in self._completed_at.items()
            if now - ts > _SESSION_TTL_SECONDS
        ]
        for tid in expired:
            self._sessions.pop(tid, None)
            self._completed_at.pop(tid, None)


def parse_rt_args(
    args: str,
    rt_config: RoundtableConfig,
) -> tuple[str, int, str | None]:
    """Parse ``!rt "topic" --rounds N``.

    Returns (topic, rounds, error_message | None).
    """
    try:
        tokens = shlex.split(args)
    except ValueError as exc:
        return "", 0, f"Parse error: {exc}"

    if not tokens:
        return "", 0, None  # show usage

    topic_parts: list[str] = []
    rounds = rt_config.rounds
    i = 0
    while i < len(tokens):
        if tokens[i] == "--rounds" and i + 1 < len(tokens):
            try:
                rounds = int(tokens[i + 1])
            except ValueError:
                return "", 0, f"Invalid rounds value: `{tokens[i + 1]}`"
            i += 2
            continue
        topic_parts.append(tokens[i])
        i += 1

    topic = " ".join(topic_parts).strip()
    if not topic:
        return "", 0, None  # show usage

    if rounds < 1:
        return "", 0, "Rounds must be at least 1."
    if rounds > rt_config.max_rounds:
        return "", 0, f"Maximum {rt_config.max_rounds} rounds allowed."

    return topic, rounds, None


def parse_followup_args(
    args: str,
    available_engines: list[str],
) -> tuple[str, list[str] | None, str | None]:
    """Parse ``!rt follow [engines] "topic"``.

    Returns (topic, engines_filter | None, error_message | None).

    If the first token (comma-separated) consists entirely of known engine
    names, it is treated as an engine filter.  Otherwise the entire input
    is treated as the topic.
    """
    try:
        tokens = shlex.split(args)
    except ValueError as exc:
        return "", None, f"Parse error: {exc}"

    if not tokens:
        return "", None, None  # show usage

    # Check if first token is an engine filter
    first = tokens[0]
    candidates = [c.strip().lower() for c in first.split(",") if c.strip()]
    engine_set = {e.lower() for e in available_engines}

    if candidates and all(c in engine_set for c in candidates):
        # Map back to original casing
        engine_map = {e.lower(): e for e in available_engines}
        engines_filter = [engine_map[c] for c in candidates]
        topic = " ".join(tokens[1:]).strip()
    else:
        engines_filter = None
        topic = " ".join(tokens).strip()

    if not topic:
        return "", engines_filter, None  # show usage

    return topic, engines_filter, None


def _build_round_prompt(
    topic: str,
    transcript: list[tuple[str, str]],
    round_num: int,
    current_round_responses: list[tuple[str, str]] | None = None,
) -> str:
    """Build the prompt for a given round.

    Includes previous rounds' transcript and any same-round responses
    that have been collected so far.
    """
    sections: list[str] = []

    # Previous rounds context
    if transcript:
        context_lines: list[str] = []
        for engine, answer in transcript:
            trimmed = answer[:_MAX_ANSWER_LENGTH] + "..." if len(answer) > _MAX_ANSWER_LENGTH else answer
            context_lines.append(f"**[{engine}]**:\n{trimmed}")
        sections.append("이전 라운드 응답:\n\n" + "\n\n".join(context_lines))

    # Same-round earlier responses
    if current_round_responses:
        current_lines: list[str] = []
        for engine, answer in current_round_responses:
            trimmed = answer[:_MAX_ANSWER_LENGTH] + "..." if len(answer) > _MAX_ANSWER_LENGTH else answer
            current_lines.append(f"**[{engine}]**:\n{trimmed}")
        sections.append(
            "이번 라운드 다른 에이전트 답변:\n\n" + "\n\n".join(current_lines)
        )

    if not sections:
        return topic

    context_block = "\n\n---\n\n".join(sections)
    return f"{context_block}\n\n---\n\n위 의견들을 참고하여 답변해주세요: {topic}"


async def _run_single_round(
    session: RoundtableSession,
    topic: str,
    engines: list[str],
    *,
    cfg: MattermostBridgeConfig,
    running_tasks: RunningTasks,
    ambient_context: RunContext | None,
) -> list[tuple[str, str]]:
    """Run one round of agents and return the round transcript."""
    # Error boundary policy:
    # - Runner unavailable (resolve_runner.issue): warn user, skip engine, continue round
    # - CWD resolution failure: warn user, skip engine, continue round
    # - handle_message() failure: log + warn user, skip engine, continue round
    # - Cancel event: break loop immediately
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
                    text=f"⚠️ **[{engine_id}]**: {resolved.issue}",
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
                message=RenderedMessage(text=f"⚠️ {exc}"),
                options=send_opts,
            )
            continue

        if cwd:
            bind_run_context(project=context.project if context else None)

        # Engine label in context line
        engine_label = f"`🤖 {engine_id}`"
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
                    text=f"⚠️ **[{engine_id}]** error: {exc}",
                ),
                options=send_opts,
            )

    return round_transcript


async def run_roundtable(
    session: RoundtableSession,
    *,
    cfg: MattermostBridgeConfig,
    chat_prefs: ChatPrefsStore | None,
    running_tasks: RunningTasks,
    ambient_context: RunContext | None,
) -> None:
    """Run all rounds of a roundtable session."""
    transport = cfg.exec_cfg.transport
    send_opts = SendOptions(thread_id=session.thread_id)

    for round_num in range(1, session.total_rounds + 1):
        if session.cancel_event.is_set():
            await transport.send(
                channel_id=session.channel_id,
                message=RenderedMessage(text="🛑 Roundtable cancelled."),
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

        round_transcript = await _run_single_round(
            session,
            session.topic,
            session.engines,
            cfg=cfg,
            running_tasks=running_tasks,
            ambient_context=ambient_context,
        )
        session.transcript.extend(round_transcript)

    # Completion marker
    if not session.cancel_event.is_set():
        rounds_label = f"{session.current_round}/{session.total_rounds} rounds"
        await transport.send(
            channel_id=session.channel_id,
            message=RenderedMessage(
                text=f"🏁 **Roundtable 완료** ({rounds_label})",
            ),
            options=send_opts,
        )


async def run_followup_round(
    session: RoundtableSession,
    followup_topic: str,
    engines_filter: list[str] | None,
    *,
    cfg: MattermostBridgeConfig,
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
    )
    session.transcript.extend(round_transcript)

    session.completed = True

    await transport.send(
        channel_id=session.channel_id,
        message=RenderedMessage(
            text=f"🏁 **Follow-up 완료** (Round {session.current_round})",
        ),
        options=send_opts,
    )
