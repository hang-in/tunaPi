"""Tests for per-user session isolation in DiscordStateStore."""

from __future__ import annotations

from pathlib import Path
import pytest

from tunapi.discord.state import DiscordStateStore
from tunapi.discord.types import DiscordChannelContext, DiscordThreadContext


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


class TestDiscordStateStore:
    @pytest.mark.anyio
    async def test_get_context_empty(self, tmp_path: Path):
        store = DiscordStateStore(config_path=tmp_path / "tunapi.toml")
        result = await store.get_context(1, 100)
        assert result is None

    @pytest.mark.anyio
    async def test_set_and_get_channel_context(self, tmp_path: Path):
        store = DiscordStateStore(config_path=tmp_path / "tunapi.toml")
        ctx = DiscordChannelContext(
            project="myproject",
            worktrees_dir=".worktrees",
            default_engine="claude",
            worktree_base="main",
        )
        await store.set_context(1, 100, ctx)
        got = await store.get_context(1, 100)
        assert isinstance(got, DiscordChannelContext)
        assert got.project == "myproject"

    @pytest.mark.anyio
    async def test_set_and_get_thread_context(self, tmp_path: Path):
        store = DiscordStateStore(config_path=tmp_path / "tunapi.toml")
        ctx = DiscordThreadContext(
            project="myproject",
            branch="feature-1",
            worktrees_dir=".worktrees",
            default_engine="claude",
        )
        await store.set_context(1, 200, ctx)
        got = await store.get_context(1, 200)
        assert isinstance(got, DiscordThreadContext)
        assert got.branch == "feature-1"

    @pytest.mark.anyio
    async def test_clear_context(self, tmp_path: Path):
        store = DiscordStateStore(config_path=tmp_path / "tunapi.toml")
        ctx = DiscordChannelContext(
            project="proj",
            worktrees_dir=".wt",
            default_engine="claude",
            worktree_base="main",
        )
        await store.set_context(1, 100, ctx)
        await store.set_context(1, 100, None)
        got = await store.get_context(1, 100)
        assert got is None

    @pytest.mark.anyio
    async def test_session_crud(self, tmp_path: Path):
        store = DiscordStateStore(config_path=tmp_path / "tunapi.toml")
        # Set session
        await store.set_session(1, 100, "claude", "tok123")
        got = await store.get_session(1, 100, "claude")
        assert got == "tok123"
        # Clear session
        await store.set_session(1, 100, "claude", None)
        got = await store.get_session(1, 100, "claude")
        assert got is None

    @pytest.mark.anyio
    async def test_session_with_author(self, tmp_path: Path):
        store = DiscordStateStore(config_path=tmp_path / "tunapi.toml")
        await store.set_session(1, 100, "claude", "tok1", author_id=42)
        got = await store.get_session(1, 100, "claude", author_id=42)
        assert got == "tok1"
        # Different author = different key
        got2 = await store.get_session(1, 100, "claude", author_id=99)
        assert got2 is None

    @pytest.mark.anyio
    async def test_get_session_missing(self, tmp_path: Path):
        store = DiscordStateStore(config_path=tmp_path / "tunapi.toml")
        got = await store.get_session(1, 100, "claude")
        assert got is None

    @pytest.mark.anyio
    async def test_clear_channel(self, tmp_path: Path):
        store = DiscordStateStore(config_path=tmp_path / "tunapi.toml")
        await store.set_session(1, 100, "claude", "tok1")
        await store.set_session(1, 100, "claude", "tok2", author_id=42)
        await store.clear_channel(1, 100)
        assert await store.get_session(1, 100, "claude") is None
        assert await store.get_session(1, 100, "claude", author_id=42) is None

    @pytest.mark.anyio
    async def test_clear_sessions(self, tmp_path: Path):
        store = DiscordStateStore(config_path=tmp_path / "tunapi.toml")
        await store.set_session(1, 100, "claude", "tok1")
        await store.clear_sessions(1, 100)
        assert await store.get_session(1, 100, "claude") is None

    @pytest.mark.anyio
    async def test_clear_sessions_with_author(self, tmp_path: Path):
        store = DiscordStateStore(config_path=tmp_path / "tunapi.toml")
        await store.set_session(1, 100, "claude", "tok1", author_id=42)
        await store.clear_sessions(1, 100, author_id=42)
        assert await store.get_session(1, 100, "claude", author_id=42) is None

    @pytest.mark.anyio
    async def test_startup_channel(self, tmp_path: Path):
        store = DiscordStateStore(config_path=tmp_path / "tunapi.toml")
        assert await store.get_startup_channel(1) is None
        await store.set_startup_channel(1, 500)
        assert await store.get_startup_channel(1) == 500
        await store.set_startup_channel(1, None)
        assert await store.get_startup_channel(1) is None

    @pytest.mark.anyio
    async def test_no_guild_id(self, tmp_path: Path):
        store = DiscordStateStore(config_path=tmp_path / "tunapi.toml")
        await store.set_session(None, 100, "claude", "tok1")
        got = await store.get_session(None, 100, "claude")
        assert got == "tok1"

    @pytest.mark.anyio
    async def test_corrupt_file(self, tmp_path: Path):
        state_path = tmp_path / "discord_state.json"
        state_path.write_text("not valid json {{{")
        store = DiscordStateStore(config_path=tmp_path / "tunapi.toml")
        got = await store.get_context(1, 100)
        assert got is None

    @pytest.mark.anyio
    async def test_context_without_project(self, tmp_path: Path):
        """Context dict exists but has no project key."""
        store = DiscordStateStore(config_path=tmp_path / "tunapi.toml")
        # Manually create invalid state
        from tunapi.discord.state import DiscordChannelStateData, DiscordState
        import msgspec

        state = DiscordState(
            channels={"1:100": DiscordChannelStateData(context={"no_project": "x"})}
        )
        state_path = tmp_path / "discord_state.json"
        import json as json_mod

        payload = msgspec.to_builtins(state)
        state_path.write_text(json_mod.dumps(payload))
        got = await store.get_context(1, 100)
        assert got is None
