"""Tests for per-user session isolation in DiscordStateStore."""

from __future__ import annotations

import pytest

from tunapi.discord.state import DiscordStateStore
from tunapi.discord.types import DiscordChannelContext


@pytest.mark.anyio
async def test_user_sessions_are_isolated(tmp_path) -> None:
    config_path = tmp_path / "tunapi.toml"
    store = DiscordStateStore(config_path=config_path)

    await store.set_session(123, 555, "claude", "tok-a", author_id=1)
    await store.set_session(123, 555, "claude", "tok-b", author_id=2)

    assert await store.get_session(123, 555, "claude", author_id=1) == "tok-a"
    assert await store.get_session(123, 555, "claude", author_id=2) == "tok-b"
    assert await store.get_session(123, 555, "claude", author_id=3) is None


@pytest.mark.anyio
async def test_clear_sessions_clears_only_one_user(tmp_path) -> None:
    config_path = tmp_path / "tunapi.toml"
    store = DiscordStateStore(config_path=config_path)

    await store.set_session(123, 555, "claude", "tok-a", author_id=1)
    await store.set_session(123, 555, "claude", "tok-b", author_id=2)

    await store.clear_sessions(123, 555, author_id=1)

    assert await store.get_session(123, 555, "claude", author_id=1) is None
    assert await store.get_session(123, 555, "claude", author_id=2) == "tok-b"


@pytest.mark.anyio
async def test_clear_sessions_all_preserves_context(tmp_path) -> None:
    config_path = tmp_path / "tunapi.toml"
    store = DiscordStateStore(config_path=config_path)

    await store.set_context(123, 555, DiscordChannelContext(project="~/dev/example"))
    await store.set_session(123, 555, "claude", "tok-a", author_id=1)
    await store.set_session(123, 555, "claude", "tok-b", author_id=2)

    await store.clear_sessions(123, 555)

    assert await store.get_context(123, 555) is not None
    assert await store.get_session(123, 555, "claude", author_id=1) is None
    assert await store.get_session(123, 555, "claude", author_id=2) is None


@pytest.mark.anyio
async def test_clear_channel_removes_context_and_user_sessions(tmp_path) -> None:
    config_path = tmp_path / "tunapi.toml"
    store = DiscordStateStore(config_path=config_path)

    await store.set_context(123, 555, DiscordChannelContext(project="~/dev/example"))
    await store.set_session(123, 555, "claude", "tok-a", author_id=1)

    await store.clear_channel(123, 555)

    assert await store.get_context(123, 555) is None
    assert await store.get_session(123, 555, "claude", author_id=1) is None
