"""Tests for /ctx command handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tunapi.discord.types import DiscordChannelContext, DiscordThreadContext


class DummyThread:
    """Minimal stand-in for discord.Thread for unit tests."""

    def __init__(self, *, parent_id: int | None) -> None:
        self.parent_id = parent_id


@pytest.mark.anyio
async def test_ctx_show_in_thread_reports_bound_and_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tunapi.discord.handlers as handlers

    monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

    ctx = MagicMock()
    ctx.guild = MagicMock()
    ctx.guild.id = 1
    ctx.channel_id = 10  # thread id
    ctx.channel = DummyThread(parent_id=20)  # parent channel id
    ctx.respond = AsyncMock()

    channel_ctx = DiscordChannelContext(
        project="/repo",
        worktrees_dir=".worktrees",
        default_engine="claude",
        worktree_base="main",
    )
    thread_ctx = DiscordThreadContext(
        project="/repo",
        branch="feat-1",
        worktrees_dir=".worktrees",
        default_engine="codex",
    )

    async def get_context(_guild_id: int, channel_id: int):
        if channel_id == 10:
            return thread_ctx
        if channel_id == 20:
            return channel_ctx
        return None

    state_store = MagicMock()
    state_store.get_context = AsyncMock(side_effect=get_context)
    state_store.set_context = AsyncMock()

    await handlers._handle_ctx_command(
        ctx,
        action=None,
        project=None,
        branch=None,
        state_store=state_store,
    )

    args, kwargs = ctx.respond.call_args
    content = args[0] if args else kwargs["content"]
    assert "**Resolved**" in content
    assert "`/repo`" in content
    assert "`feat-1`" in content
    assert "`codex`" in content
    assert "**Bound**" in content


@pytest.mark.anyio
async def test_ctx_show_in_thread_falls_back_to_channel_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tunapi.discord.handlers as handlers

    monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

    ctx = MagicMock()
    ctx.guild = MagicMock()
    ctx.guild.id = 1
    ctx.channel_id = 10  # thread id
    ctx.channel = DummyThread(parent_id=20)  # parent channel id
    ctx.respond = AsyncMock()

    channel_ctx = DiscordChannelContext(
        project="/repo",
        worktrees_dir=".worktrees",
        default_engine="claude",
        worktree_base="main",
    )

    async def get_context(_guild_id: int, channel_id: int):
        if channel_id == 10:
            return None
        if channel_id == 20:
            return channel_ctx
        return None

    state_store = MagicMock()
    state_store.get_context = AsyncMock(side_effect=get_context)
    state_store.set_context = AsyncMock()

    await handlers._handle_ctx_command(
        ctx,
        action="show",
        project=None,
        branch=None,
        state_store=state_store,
    )

    args, kwargs = ctx.respond.call_args
    content = args[0] if args else kwargs["content"]
    assert "`/repo`" in content
    assert "`main`" in content
    assert "Source: channel" in content


@pytest.mark.anyio
async def test_ctx_set_in_thread_rebinds_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tunapi.discord.handlers as handlers

    monkeypatch.setattr(handlers.discord, "Thread", DummyThread)
    monkeypatch.setattr(handlers, "_require_admin", AsyncMock(return_value=True))

    ctx = MagicMock()
    ctx.guild = MagicMock()
    ctx.guild.id = 1
    ctx.channel_id = 10  # thread id
    ctx.channel = DummyThread(parent_id=20)  # parent channel id
    ctx.respond = AsyncMock()

    channel_ctx = DiscordChannelContext(
        project="/repo",
        worktrees_dir=".worktrees",
        default_engine="claude",
        worktree_base="main",
    )

    async def get_context(_guild_id: int, channel_id: int):
        if channel_id == 10:
            return None
        if channel_id == 20:
            return channel_ctx
        return None

    state_store = MagicMock()
    state_store.get_context = AsyncMock(side_effect=get_context)
    state_store.set_context = AsyncMock()

    await handlers._handle_ctx_command(
        ctx,
        action="set",
        project=None,
        branch="@feature/new",
        state_store=state_store,
    )

    state_store.set_context.assert_awaited_once_with(
        1,
        10,
        DiscordThreadContext(
            project="/repo",
            branch="feature/new",
            worktrees_dir=".worktrees",
            default_engine="claude",
        ),
    )


@pytest.mark.anyio
async def test_ctx_set_in_channel_updates_project_and_base_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tunapi.discord.handlers as handlers

    monkeypatch.setattr(handlers.discord, "Thread", DummyThread)
    monkeypatch.setattr(handlers, "_require_admin", AsyncMock(return_value=True))

    ctx = MagicMock()
    ctx.guild = MagicMock()
    ctx.guild.id = 1
    ctx.channel_id = 20  # channel id
    ctx.channel = MagicMock()
    ctx.respond = AsyncMock()

    existing = DiscordChannelContext(
        project="/old",
        worktrees_dir=".wt",
        default_engine="codex",
        worktree_base="main",
    )

    state_store = MagicMock()
    state_store.get_context = AsyncMock(return_value=existing)
    state_store.set_context = AsyncMock()

    await handlers._handle_ctx_command(
        ctx,
        action="set",
        project="/new",
        branch="@dev",
        state_store=state_store,
    )

    state_store.set_context.assert_awaited_once_with(
        1,
        20,
        DiscordChannelContext(
            project="/new",
            worktrees_dir=".wt",
            default_engine="codex",
            worktree_base="dev",
        ),
    )
