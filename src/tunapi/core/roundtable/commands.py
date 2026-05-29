"""Roundtable argument parsing + transport-agnostic ``!rt`` handler."""

from __future__ import annotations

import shlex
from collections.abc import Awaitable, Callable

from ...transport import RenderedMessage
from ...transport_runtime import RoundtableConfig, TransportRuntime


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


async def handle_rt(
    args: str,
    *,
    runtime: TransportRuntime,
    send: Callable[[RenderedMessage], Awaitable[None]],
    start_roundtable: Callable[[str, int, list[str]], Awaitable[None]],
    continue_roundtable: Callable[[str, list[str] | None], Awaitable[None]]
    | None = None,
    close_roundtable: Callable[[], Awaitable[None]] | None = None,
    thread_id: str | None = None,
) -> None:
    """Handle ``!rt`` commands.

    - ``!rt "topic" [--rounds N]`` — start a new roundtable
    - ``!rt follow [engines] "topic"`` — follow-up in completed roundtable thread
    - ``!rt close`` — close the current roundtable thread
    """
    rt_config = runtime.roundtable
    rt_engines = list(rt_config.engines) or list(runtime.available_engine_ids())

    if not rt_engines:
        await send(RenderedMessage(text="No engines available for roundtable."))
        return

    stripped = args.strip()

    # Check for "close" subcommand
    if stripped.lower().startswith("close"):
        if not close_roundtable:
            await send(
                RenderedMessage(
                    text="`!rt close` can only be used inside a roundtable thread."
                )
            )
            return
        await close_roundtable()
        return

    # Check for "follow" subcommand
    if stripped.lower().startswith("follow"):
        follow_args = stripped[len("follow") :].strip()
        if not continue_roundtable:
            await send(
                RenderedMessage(
                    text="`!rt follow` can only be used inside a completed roundtable thread."
                )
            )
            return

        topic, engines_filter, error = parse_followup_args(follow_args, rt_engines)
        if error:
            await send(RenderedMessage(text=f"{error}"))
            return
        if not topic:
            engines_display = ", ".join(f"`{e}`" for e in rt_engines)
            await send(
                RenderedMessage(
                    text=(
                        "*Roundtable Follow-up*\n\n"
                        "Usage:\n"
                        '- `!rt follow "question"` — all engines\n'
                        '- `!rt follow claude "question"` — specific engine\n'
                        '- `!rt follow gemini,claude "question"` — multiple engines\n\n'
                        f"Engines: {engines_display}"
                    )
                )
            )
            return

        await continue_roundtable(topic, engines_filter)
        return

    # Default: start a new roundtable
    topic, rounds, error = parse_rt_args(args, rt_config)

    if error:
        await send(RenderedMessage(text=f"{error}"))
        return
    if not topic:
        engines_display = ", ".join(f"`{e}`" for e in rt_engines)
        await send(
            RenderedMessage(
                text=(
                    "*Roundtable* — collect opinions from multiple agents\n\n"
                    "Usage:\n"
                    '- `!rt "topic"` — new roundtable\n'
                    '- `!rt "topic" --rounds 2` — multi-round\n'
                    '- `!rt follow [engines] "question"` — follow-up\n'
                    "- `!rt close` — close roundtable\n\n"
                    f"Engines: {engines_display}\n"
                    f"Default rounds: {rt_config.rounds} (max {rt_config.max_rounds})"
                )
            )
        )
        return

    await start_roundtable(topic, rounds, rt_engines)
