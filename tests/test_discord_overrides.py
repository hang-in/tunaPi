"""Comprehensive tests for discord/overrides.py.

Covers:
- resolve_overrides() — model/reasoning resolution with cascading precedence
- resolve_trigger_mode() — default, channel override, thread override precedence
- resolve_default_engine() — thread > channel > config fallback
- resolve_effective_default_engine() — thread > channel > bound context > config fallback
- supports_reasoning() / is_valid_reasoning_level() — helper predicates
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tunapi.discord.overrides import (
    REASONING_ENGINES,
    REASONING_LEVELS,
    ResolvedOverrides,
    is_valid_reasoning_level,
    resolve_default_engine,
    resolve_effective_default_engine,
    resolve_overrides,
    resolve_trigger_mode,
    supports_reasoning,
)
from tunapi.discord.prefs import DiscordPrefsStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GUILD = 100
CHANNEL = 200
THREAD = 300


@pytest.fixture
def store(tmp_path: Path) -> DiscordPrefsStore:
    return DiscordPrefsStore(tmp_path / "tunapi.toml")


# ===========================================================================
# resolve_overrides
# ===========================================================================


class TestResolveOverrides:
    """Tests for resolve_overrides()."""

    @pytest.mark.anyio
    async def test_no_overrides_returns_all_none(self, store: DiscordPrefsStore) -> None:
        result = await resolve_overrides(store, GUILD, CHANNEL, None, "claude")
        assert result == ResolvedOverrides()

    @pytest.mark.anyio
    async def test_no_overrides_with_thread_returns_all_none(
        self, store: DiscordPrefsStore
    ) -> None:
        result = await resolve_overrides(store, GUILD, CHANNEL, THREAD, "claude")
        assert result.model is None
        assert result.reasoning is None
        assert result.source_model is None
        assert result.source_reasoning is None

    @pytest.mark.anyio
    async def test_channel_model_override(self, store: DiscordPrefsStore) -> None:
        await store.set_model_override(GUILD, CHANNEL, "claude", "opus-4")
        result = await resolve_overrides(store, GUILD, CHANNEL, None, "claude")
        assert result.model == "opus-4"
        assert result.source_model == "channel"
        assert result.reasoning is None
        assert result.source_reasoning is None

    @pytest.mark.anyio
    async def test_channel_reasoning_override(self, store: DiscordPrefsStore) -> None:
        await store.set_reasoning_override(GUILD, CHANNEL, "codex", "high")
        result = await resolve_overrides(store, GUILD, CHANNEL, None, "codex")
        assert result.reasoning == "high"
        assert result.source_reasoning == "channel"
        assert result.model is None

    @pytest.mark.anyio
    async def test_thread_model_overrides_channel(
        self, store: DiscordPrefsStore
    ) -> None:
        await store.set_model_override(GUILD, CHANNEL, "claude", "sonnet-4")
        await store.set_model_override(GUILD, THREAD, "claude", "opus-4")
        result = await resolve_overrides(store, GUILD, CHANNEL, THREAD, "claude")
        assert result.model == "opus-4"
        assert result.source_model == "thread"

    @pytest.mark.anyio
    async def test_thread_reasoning_overrides_channel(
        self, store: DiscordPrefsStore
    ) -> None:
        await store.set_reasoning_override(GUILD, CHANNEL, "codex", "low")
        await store.set_reasoning_override(GUILD, THREAD, "codex", "xhigh")
        result = await resolve_overrides(store, GUILD, CHANNEL, THREAD, "codex")
        assert result.reasoning == "xhigh"
        assert result.source_reasoning == "thread"

    @pytest.mark.anyio
    async def test_thread_model_falls_back_to_channel(
        self, store: DiscordPrefsStore
    ) -> None:
        """Thread has no model override -> falls back to channel."""
        await store.set_model_override(GUILD, CHANNEL, "claude", "sonnet-4")
        result = await resolve_overrides(store, GUILD, CHANNEL, THREAD, "claude")
        assert result.model == "sonnet-4"
        assert result.source_model == "channel"

    @pytest.mark.anyio
    async def test_thread_reasoning_falls_back_to_channel(
        self, store: DiscordPrefsStore
    ) -> None:
        await store.set_reasoning_override(GUILD, CHANNEL, "codex", "medium")
        result = await resolve_overrides(store, GUILD, CHANNEL, THREAD, "codex")
        assert result.reasoning == "medium"
        assert result.source_reasoning == "channel"

    @pytest.mark.anyio
    async def test_mixed_sources_model_thread_reasoning_channel(
        self, store: DiscordPrefsStore
    ) -> None:
        """Model from thread, reasoning from channel (independent resolution)."""
        await store.set_model_override(GUILD, THREAD, "codex", "o3")
        await store.set_reasoning_override(GUILD, CHANNEL, "codex", "high")
        result = await resolve_overrides(store, GUILD, CHANNEL, THREAD, "codex")
        assert result.model == "o3"
        assert result.source_model == "thread"
        assert result.reasoning == "high"
        assert result.source_reasoning == "channel"

    @pytest.mark.anyio
    async def test_different_engine_ids_are_isolated(
        self, store: DiscordPrefsStore
    ) -> None:
        await store.set_model_override(GUILD, CHANNEL, "claude", "opus-4")
        result = await resolve_overrides(store, GUILD, CHANNEL, None, "codex")
        assert result.model is None

    @pytest.mark.anyio
    async def test_both_channel_overrides_set(self, store: DiscordPrefsStore) -> None:
        await store.set_model_override(GUILD, CHANNEL, "codex", "o3")
        await store.set_reasoning_override(GUILD, CHANNEL, "codex", "medium")
        result = await resolve_overrides(store, GUILD, CHANNEL, None, "codex")
        assert result.model == "o3"
        assert result.source_model == "channel"
        assert result.reasoning == "medium"
        assert result.source_reasoning == "channel"


# ===========================================================================
# resolve_trigger_mode
# ===========================================================================


class TestResolveTriggerMode:
    """Tests for resolve_trigger_mode()."""

    @pytest.mark.anyio
    async def test_default_mode_all(self, store: DiscordPrefsStore) -> None:
        mode = await resolve_trigger_mode(store, GUILD, CHANNEL, None)
        assert mode == "all"

    @pytest.mark.anyio
    async def test_default_mode_mentions(self, store: DiscordPrefsStore) -> None:
        mode = await resolve_trigger_mode(
            store, GUILD, CHANNEL, None, default_mode="mentions"
        )
        assert mode == "mentions"

    @pytest.mark.anyio
    async def test_channel_override(self, store: DiscordPrefsStore) -> None:
        await store.set_trigger_mode(GUILD, CHANNEL, "mentions")
        mode = await resolve_trigger_mode(store, GUILD, CHANNEL, None)
        assert mode == "mentions"

    @pytest.mark.anyio
    async def test_channel_override_beats_default(
        self, store: DiscordPrefsStore
    ) -> None:
        await store.set_trigger_mode(GUILD, CHANNEL, "all")
        mode = await resolve_trigger_mode(
            store, GUILD, CHANNEL, None, default_mode="mentions"
        )
        assert mode == "all"

    @pytest.mark.anyio
    async def test_thread_override_beats_channel(
        self, store: DiscordPrefsStore
    ) -> None:
        await store.set_trigger_mode(GUILD, CHANNEL, "all")
        await store.set_trigger_mode(GUILD, THREAD, "mentions")
        mode = await resolve_trigger_mode(store, GUILD, CHANNEL, THREAD)
        assert mode == "mentions"

    @pytest.mark.anyio
    async def test_thread_override_beats_default(
        self, store: DiscordPrefsStore
    ) -> None:
        await store.set_trigger_mode(GUILD, THREAD, "mentions")
        mode = await resolve_trigger_mode(
            store, GUILD, CHANNEL, THREAD, default_mode="all"
        )
        assert mode == "mentions"

    @pytest.mark.anyio
    async def test_no_thread_falls_to_channel(self, store: DiscordPrefsStore) -> None:
        """Thread ID provided but no thread override -> falls to channel."""
        await store.set_trigger_mode(GUILD, CHANNEL, "mentions")
        mode = await resolve_trigger_mode(store, GUILD, CHANNEL, THREAD)
        assert mode == "mentions"

    @pytest.mark.anyio
    async def test_no_thread_no_channel_falls_to_default(
        self, store: DiscordPrefsStore
    ) -> None:
        mode = await resolve_trigger_mode(
            store, GUILD, CHANNEL, THREAD, default_mode="mentions"
        )
        assert mode == "mentions"


# ===========================================================================
# resolve_default_engine
# ===========================================================================


class TestResolveDefaultEngine:
    """Tests for resolve_default_engine()."""

    @pytest.mark.anyio
    async def test_no_overrides_returns_config(self, store: DiscordPrefsStore) -> None:
        engine, source = await resolve_default_engine(
            store, GUILD, CHANNEL, None, "claude"
        )
        assert engine == "claude"
        assert source == "config"

    @pytest.mark.anyio
    async def test_no_overrides_no_config_returns_none(
        self, store: DiscordPrefsStore
    ) -> None:
        engine, source = await resolve_default_engine(
            store, GUILD, CHANNEL, None, None
        )
        assert engine is None
        assert source is None

    @pytest.mark.anyio
    async def test_channel_override(self, store: DiscordPrefsStore) -> None:
        await store.set_default_engine(GUILD, CHANNEL, "codex")
        engine, source = await resolve_default_engine(
            store, GUILD, CHANNEL, None, "claude"
        )
        assert engine == "codex"
        assert source == "channel"

    @pytest.mark.anyio
    async def test_thread_override_beats_channel(
        self, store: DiscordPrefsStore
    ) -> None:
        await store.set_default_engine(GUILD, CHANNEL, "codex")
        await store.set_default_engine(GUILD, THREAD, "gemini")
        engine, source = await resolve_default_engine(
            store, GUILD, CHANNEL, THREAD, "claude"
        )
        assert engine == "gemini"
        assert source == "thread"

    @pytest.mark.anyio
    async def test_thread_no_override_falls_to_channel(
        self, store: DiscordPrefsStore
    ) -> None:
        await store.set_default_engine(GUILD, CHANNEL, "codex")
        engine, source = await resolve_default_engine(
            store, GUILD, CHANNEL, THREAD, "claude"
        )
        assert engine == "codex"
        assert source == "channel"

    @pytest.mark.anyio
    async def test_thread_no_override_no_channel_falls_to_config(
        self, store: DiscordPrefsStore
    ) -> None:
        engine, source = await resolve_default_engine(
            store, GUILD, CHANNEL, THREAD, "claude"
        )
        assert engine == "claude"
        assert source == "config"

    @pytest.mark.anyio
    async def test_channel_override_beats_config(
        self, store: DiscordPrefsStore
    ) -> None:
        await store.set_default_engine(GUILD, CHANNEL, "gemini")
        engine, source = await resolve_default_engine(
            store, GUILD, CHANNEL, None, "claude"
        )
        assert engine == "gemini"
        assert source == "channel"


# ===========================================================================
# resolve_effective_default_engine
# ===========================================================================


class TestResolveEffectiveDefaultEngine:
    """Tests for resolve_effective_default_engine() with bound context defaults."""

    @pytest.mark.anyio
    async def test_all_none(self, store: DiscordPrefsStore) -> None:
        engine, source = await resolve_effective_default_engine(
            store,
            guild_id=GUILD,
            channel_id=CHANNEL,
            thread_id=None,
            bound_thread_default=None,
            bound_channel_default=None,
            config_default=None,
        )
        assert engine is None
        assert source is None

    @pytest.mark.anyio
    async def test_config_fallback(self, store: DiscordPrefsStore) -> None:
        engine, source = await resolve_effective_default_engine(
            store,
            guild_id=GUILD,
            channel_id=CHANNEL,
            thread_id=None,
            bound_thread_default=None,
            bound_channel_default=None,
            config_default="claude",
        )
        assert engine == "claude"
        assert source == "config"

    @pytest.mark.anyio
    async def test_bound_channel_beats_config(self, store: DiscordPrefsStore) -> None:
        engine, source = await resolve_effective_default_engine(
            store,
            guild_id=GUILD,
            channel_id=CHANNEL,
            thread_id=None,
            bound_thread_default=None,
            bound_channel_default="codex",
            config_default="claude",
        )
        assert engine == "codex"
        assert source == "channel_context"

    @pytest.mark.anyio
    async def test_bound_thread_beats_bound_channel(
        self, store: DiscordPrefsStore
    ) -> None:
        engine, source = await resolve_effective_default_engine(
            store,
            guild_id=GUILD,
            channel_id=CHANNEL,
            thread_id=THREAD,
            bound_thread_default="gemini",
            bound_channel_default="codex",
            config_default="claude",
        )
        assert engine == "gemini"
        assert source == "thread_context"

    @pytest.mark.anyio
    async def test_channel_override_beats_bound_context(
        self, store: DiscordPrefsStore
    ) -> None:
        await store.set_default_engine(GUILD, CHANNEL, "pi")
        engine, source = await resolve_effective_default_engine(
            store,
            guild_id=GUILD,
            channel_id=CHANNEL,
            thread_id=None,
            bound_thread_default=None,
            bound_channel_default="codex",
            config_default="claude",
        )
        assert engine == "pi"
        assert source == "channel_override"

    @pytest.mark.anyio
    async def test_thread_override_beats_everything(
        self, store: DiscordPrefsStore
    ) -> None:
        await store.set_default_engine(GUILD, CHANNEL, "pi")
        await store.set_default_engine(GUILD, THREAD, "opencode")
        engine, source = await resolve_effective_default_engine(
            store,
            guild_id=GUILD,
            channel_id=CHANNEL,
            thread_id=THREAD,
            bound_thread_default="gemini",
            bound_channel_default="codex",
            config_default="claude",
        )
        assert engine == "opencode"
        assert source == "thread_override"

    @pytest.mark.anyio
    async def test_no_thread_id_skips_thread_override(
        self, store: DiscordPrefsStore
    ) -> None:
        """When thread_id is None, thread override lookup is skipped entirely."""
        await store.set_default_engine(GUILD, CHANNEL, "codex")
        engine, source = await resolve_effective_default_engine(
            store,
            guild_id=GUILD,
            channel_id=CHANNEL,
            thread_id=None,
            bound_thread_default="gemini",
            bound_channel_default=None,
            config_default="claude",
        )
        assert engine == "codex"
        assert source == "channel_override"

    @pytest.mark.anyio
    async def test_bound_thread_ignored_when_no_thread_id(
        self, store: DiscordPrefsStore
    ) -> None:
        """bound_thread_default is still checked even if thread_id is None
        (it's from context binding, not prefs).
        """
        engine, source = await resolve_effective_default_engine(
            store,
            guild_id=GUILD,
            channel_id=CHANNEL,
            thread_id=None,
            bound_thread_default="gemini",
            bound_channel_default=None,
            config_default="claude",
        )
        assert engine == "gemini"
        assert source == "thread_context"


# ===========================================================================
# Helper predicates
# ===========================================================================


class TestSupportReasoning:
    """Tests for supports_reasoning()."""

    def test_codex_supports_reasoning(self) -> None:
        assert supports_reasoning("codex") is True

    def test_claude_does_not_support_reasoning(self) -> None:
        assert supports_reasoning("claude") is False

    def test_unknown_engine(self) -> None:
        assert supports_reasoning("nonexistent") is False

    def test_all_reasoning_engines_recognized(self) -> None:
        for engine in REASONING_ENGINES:
            assert supports_reasoning(engine) is True


class TestIsValidReasoningLevel:
    """Tests for is_valid_reasoning_level()."""

    @pytest.mark.parametrize("level", sorted(REASONING_LEVELS))
    def test_valid_levels(self, level: str) -> None:
        assert is_valid_reasoning_level(level) is True

    @pytest.mark.parametrize("level", ["none", "max", "ultra", "MINIMAL", "", "High"])
    def test_invalid_levels(self, level: str) -> None:
        assert is_valid_reasoning_level(level) is False


# ===========================================================================
# ResolvedOverrides dataclass
# ===========================================================================


class TestResolvedOverridesDataclass:
    """Tests for ResolvedOverrides frozen dataclass."""

    def test_default_values(self) -> None:
        r = ResolvedOverrides()
        assert r.model is None
        assert r.reasoning is None
        assert r.source_model is None
        assert r.source_reasoning is None

    def test_custom_values(self) -> None:
        r = ResolvedOverrides(
            model="opus-4",
            reasoning="high",
            source_model="thread",
            source_reasoning="channel",
        )
        assert r.model == "opus-4"
        assert r.reasoning == "high"
        assert r.source_model == "thread"
        assert r.source_reasoning == "channel"

    def test_frozen(self) -> None:
        r = ResolvedOverrides(model="opus-4")
        with pytest.raises(AttributeError):
            r.model = "sonnet-4"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = ResolvedOverrides(model="opus-4", source_model="channel")
        b = ResolvedOverrides(model="opus-4", source_model="channel")
        assert a == b
