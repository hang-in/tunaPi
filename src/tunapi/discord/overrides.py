"""Override resolution for Discord transport.

Implements cascading override resolution: thread -> channel -> config default -> None
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .prefs import DiscordPrefsStore

# Valid reasoning levels (matching telegram transport)
REASONING_LEVELS = frozenset({"minimal", "low", "medium", "high", "xhigh"})

# Engines that support reasoning overrides
REASONING_ENGINES = frozenset({"codex"})


@dataclass(frozen=True, slots=True)
class ResolvedOverrides:
    """Resolved overrides for an engine."""

    model: str | None = None
    reasoning: str | None = None
    source_model: str | None = None  # "thread", "channel", or None
    source_reasoning: str | None = None  # "thread", "channel", or None


async def resolve_overrides(
    prefs_store: DiscordPrefsStore,
    guild_id: int,
    channel_id: int,
    thread_id: int | None,
    engine_id: str,
) -> ResolvedOverrides:
    """Resolve model and reasoning overrides with cascading precedence.

    Resolution order (first match wins):
    1. Thread override (if in a thread)
    2. Channel override
    3. None (use engine default)
    """
    model: str | None = None
    reasoning: str | None = None
    source_model: str | None = None
    source_reasoning: str | None = None

    # Check thread overrides first (if in a thread)
    if thread_id is not None:
        thread_model = await prefs_store.get_model_override(
            guild_id, thread_id, engine_id
        )
        if thread_model is not None:
            model = thread_model
            source_model = "thread"

        thread_reasoning = await prefs_store.get_reasoning_override(
            guild_id, thread_id, engine_id
        )
        if thread_reasoning is not None:
            reasoning = thread_reasoning
            source_reasoning = "thread"

    # Fall back to channel overrides
    if model is None:
        channel_model = await prefs_store.get_model_override(
            guild_id, channel_id, engine_id
        )
        if channel_model is not None:
            model = channel_model
            source_model = "channel"

    if reasoning is None:
        channel_reasoning = await prefs_store.get_reasoning_override(
            guild_id, channel_id, engine_id
        )
        if channel_reasoning is not None:
            reasoning = channel_reasoning
            source_reasoning = "channel"

    return ResolvedOverrides(
        model=model,
        reasoning=reasoning,
        source_model=source_model,
        source_reasoning=source_reasoning,
    )


async def resolve_trigger_mode(
    prefs_store: DiscordPrefsStore,
    guild_id: int,
    channel_id: int,
    thread_id: int | None,
    *,
    default_mode: Literal["all", "mentions"] = "all",
) -> Literal["all", "mentions"]:
    """Resolve trigger mode with cascading precedence.

    Resolution order (first match wins):
    1. Thread trigger mode (if in a thread)
    2. Channel trigger mode
    3. Config default
    """
    # Check thread first
    if thread_id is not None:
        thread_mode = await prefs_store.get_trigger_mode(guild_id, thread_id)
        if thread_mode is not None:
            return thread_mode

    # Fall back to channel
    channel_mode = await prefs_store.get_trigger_mode(guild_id, channel_id)
    if channel_mode is not None:
        return channel_mode

    # Default
    return default_mode


async def resolve_default_engine(
    prefs_store: DiscordPrefsStore,
    guild_id: int,
    channel_id: int,
    thread_id: int | None,
    config_default: str | None,
) -> tuple[str | None, str | None]:
    """Resolve default engine with cascading precedence.

    Resolution order (first match wins):
    1. Thread default engine (if in a thread)
    2. Channel default engine
    3. Config default

    Returns: (engine_id, source) where source is "thread", "channel", "config", or None
    """
    # Check thread first
    if thread_id is not None:
        thread_engine = await prefs_store.get_default_engine(guild_id, thread_id)
        if thread_engine is not None:
            return thread_engine, "thread"

    # Fall back to channel
    channel_engine = await prefs_store.get_default_engine(guild_id, channel_id)
    if channel_engine is not None:
        return channel_engine, "channel"

    # Config default
    if config_default is not None:
        return config_default, "config"

    return None, None


async def resolve_effective_default_engine(
    prefs_store: DiscordPrefsStore,
    *,
    guild_id: int,
    channel_id: int,
    thread_id: int | None,
    bound_thread_default: str | None,
    bound_channel_default: str | None,
    config_default: str | None,
) -> tuple[str | None, str | None]:
    """Resolve default engine including bound context defaults.

    Resolution order (first match wins):
    1. Thread override (prefs) (if in a thread)
    2. Channel override (prefs)
    3. Bound thread context default
    4. Bound channel context default
    5. Config default

    Returns: (engine_id, source) where source is one of:
      - "thread_override"
      - "channel_override"
      - "thread_context"
      - "channel_context"
      - "config"
      - None
    """
    if thread_id is not None:
        thread_engine = await prefs_store.get_default_engine(guild_id, thread_id)
        if thread_engine is not None:
            return thread_engine, "thread_override"

    channel_engine = await prefs_store.get_default_engine(guild_id, channel_id)
    if channel_engine is not None:
        return channel_engine, "channel_override"

    if bound_thread_default is not None:
        return bound_thread_default, "thread_context"

    if bound_channel_default is not None:
        return bound_channel_default, "channel_context"

    if config_default is not None:
        return config_default, "config"

    return None, None


def supports_reasoning(engine_id: str) -> bool:
    """Check if an engine supports reasoning overrides."""
    return engine_id in REASONING_ENGINES


def is_valid_reasoning_level(level: str) -> bool:
    """Check if a reasoning level is valid."""
    return level in REASONING_LEVELS
