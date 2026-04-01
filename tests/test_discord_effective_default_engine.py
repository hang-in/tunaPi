"""Tests for effective default engine resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from tunapi.discord.overrides import resolve_effective_default_engine
from tunapi.discord.prefs import DiscordPrefsStore


@pytest.mark.anyio
async def test_effective_default_engine_prefers_thread_override(tmp_path: Path) -> None:
    store = DiscordPrefsStore(tmp_path / "tunapi.toml")
    await store.set_default_engine(123, 456, "codex")  # channel
    await store.set_default_engine(123, 789, "claude")  # thread

    engine, source = await resolve_effective_default_engine(
        store,
        guild_id=123,
        channel_id=456,
        thread_id=789,
        bound_thread_default="gemini",
        bound_channel_default="pi",
        config_default="codex",
    )
    assert engine == "claude"
    assert source == "thread_override"


@pytest.mark.anyio
async def test_effective_default_engine_falls_back_to_channel_override(
    tmp_path: Path,
) -> None:
    store = DiscordPrefsStore(tmp_path / "tunapi.toml")
    await store.set_default_engine(123, 456, "codex")  # channel

    engine, source = await resolve_effective_default_engine(
        store,
        guild_id=123,
        channel_id=456,
        thread_id=789,
        bound_thread_default="gemini",
        bound_channel_default="pi",
        config_default="claude",
    )
    assert engine == "codex"
    assert source == "channel_override"


@pytest.mark.anyio
async def test_effective_default_engine_uses_bound_context_defaults(
    tmp_path: Path,
) -> None:
    store = DiscordPrefsStore(tmp_path / "tunapi.toml")

    engine, source = await resolve_effective_default_engine(
        store,
        guild_id=123,
        channel_id=456,
        thread_id=789,
        bound_thread_default="gemini",
        bound_channel_default="pi",
        config_default="claude",
    )
    assert engine == "gemini"
    assert source == "thread_context"

    engine2, source2 = await resolve_effective_default_engine(
        store,
        guild_id=123,
        channel_id=456,
        thread_id=None,
        bound_thread_default=None,
        bound_channel_default="pi",
        config_default="claude",
    )
    assert engine2 == "pi"
    assert source2 == "channel_context"


@pytest.mark.anyio
async def test_effective_default_engine_falls_back_to_config(tmp_path: Path) -> None:
    store = DiscordPrefsStore(tmp_path / "tunapi.toml")

    engine, source = await resolve_effective_default_engine(
        store,
        guild_id=123,
        channel_id=456,
        thread_id=None,
        bound_thread_default=None,
        bound_channel_default=None,
        config_default="claude",
    )
    assert engine == "claude"
    assert source == "config"
