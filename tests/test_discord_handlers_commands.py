"""Tests for slash command handlers in discord/handlers.py.

Covers:
- _require_admin (lines 41-52)
- _handle_ctx_command uncovered branches (lines 62-257)
- register_slash_commands inner handlers (lines 258-1133)
- _handle_engine_command (lines 1335-1554)
- register_engine_commands / _format_engine_starter_message
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import discord
import pytest

from tunapi.discord.overrides import ResolvedOverrides
from tunapi.discord.types import DiscordChannelContext, DiscordThreadContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DummyThread:
    """Minimal stand-in for discord.Thread."""

    def __init__(self, *, parent_id: int | None, id: int = 10) -> None:
        self.parent_id = parent_id
        self.id = id


class DummyTextChannel:
    """Minimal stand-in for discord.TextChannel."""

    def __init__(self, *, category: object | None = None) -> None:
        self.category = category


def _make_ctx(
    *,
    guild_id: int = 1,
    channel_id: int = 100,
    channel: object | None = None,
    author_id: int | None = 42,
    is_admin: bool = False,
) -> MagicMock:
    """Build a mock ApplicationContext."""
    ctx = MagicMock()
    if guild_id is not None:
        ctx.guild = MagicMock()
        ctx.guild.id = guild_id
    else:
        ctx.guild = None
    ctx.channel_id = channel_id
    ctx.channel = channel or MagicMock()
    ctx.respond = AsyncMock()
    ctx.defer = AsyncMock()
    ctx.followup = MagicMock()
    ctx.followup.send = AsyncMock()

    author = MagicMock(spec=discord.Member)
    if author_id is not None:
        author.id = author_id
    perms = MagicMock()
    perms.administrator = is_admin
    type(author).guild_permissions = PropertyMock(return_value=perms)
    ctx.author = author
    return ctx


def _make_state_store(**overrides) -> MagicMock:
    ss = MagicMock()
    ss.get_context = AsyncMock(return_value=None)
    ss.set_context = AsyncMock()
    ss.clear_channel = AsyncMock()
    ss.clear_sessions = AsyncMock()
    ss.get_session = AsyncMock(return_value=None)
    ss.set_session = AsyncMock()
    for k, v in overrides.items():
        setattr(ss, k, v)
    return ss


def _make_prefs_store(**overrides) -> MagicMock:
    ps = MagicMock()
    ps.set_default_engine = AsyncMock()
    ps.get_model_override = AsyncMock(return_value=None)
    ps.set_model_override = AsyncMock()
    ps.get_reasoning_override = AsyncMock(return_value=None)
    ps.set_reasoning_override = AsyncMock()
    ps.set_trigger_mode = AsyncMock()
    ps.get_trigger_mode = AsyncMock(return_value=None)
    ps.get_all_overrides = AsyncMock(return_value=({}, {}, None, None))
    ps.clear_channel = AsyncMock()
    for k, v in overrides.items():
        setattr(ps, k, v)
    return ps


# ===========================================================================
# _require_admin
# ===========================================================================


class TestRequireAdmin:
    @pytest.mark.anyio
    async def test_non_admin_responds_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        ctx = _make_ctx(is_admin=False)
        result = await handlers._require_admin(ctx)
        assert result is False
        ctx.respond.assert_awaited_once()
        msg = ctx.respond.call_args[0][0]
        assert "administrator" in msg.lower()

    @pytest.mark.anyio
    async def test_admin_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        ctx = _make_ctx(is_admin=True)
        result = await handlers._require_admin(ctx)
        assert result is True
        ctx.respond.assert_not_awaited()

    @pytest.mark.anyio
    async def test_no_guild_returns_false(self) -> None:
        import tunapi.discord.handlers as handlers

        ctx = _make_ctx(guild_id=None)
        # _is_admin returns False when guild is None, so _require_admin sends error
        result = await handlers._require_admin(ctx)
        assert result is False


# ===========================================================================
# _handle_ctx_command — additional branches
# ===========================================================================


class TestHandleCtxCommand:
    @pytest.mark.anyio
    async def test_no_guild_responds_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        ctx = _make_ctx(guild_id=None)
        state_store = _make_state_store()
        await handlers._handle_ctx_command(
            ctx, action="show", project=None, branch=None, state_store=state_store
        )
        msg = ctx.respond.call_args[0][0]
        assert "server" in msg.lower()

    @pytest.mark.anyio
    async def test_clear_in_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers, "_require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

        ctx = _make_ctx(channel_id=100, is_admin=True)
        state_store = _make_state_store()
        await handlers._handle_ctx_command(
            ctx, action="clear", project=None, branch=None, state_store=state_store
        )
        state_store.set_context.assert_awaited_once_with(1, 100, None)
        msg = ctx.respond.call_args[0][0]
        assert "channel" in msg.lower()

    @pytest.mark.anyio
    async def test_clear_in_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers, "_require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

        thread = DummyThread(parent_id=200, id=10)
        ctx = _make_ctx(channel_id=10)
        ctx.channel = thread
        state_store = _make_state_store()
        await handlers._handle_ctx_command(
            ctx, action="clear", project=None, branch=None, state_store=state_store
        )
        state_store.set_context.assert_awaited_once_with(1, 10, None)
        msg = ctx.respond.call_args[0][0]
        assert "thread" in msg.lower()

    @pytest.mark.anyio
    async def test_clear_requires_admin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers, "_require_admin", AsyncMock(return_value=False))
        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

        ctx = _make_ctx(channel_id=100, is_admin=False)
        state_store = _make_state_store()
        await handlers._handle_ctx_command(
            ctx, action="clear", project=None, branch=None, state_store=state_store
        )
        state_store.set_context.assert_not_awaited()

    @pytest.mark.anyio
    async def test_set_in_thread_rejects_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers, "_require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

        thread = DummyThread(parent_id=200, id=10)
        ctx = _make_ctx(channel_id=10)
        ctx.channel = thread
        state_store = _make_state_store()
        await handlers._handle_ctx_command(
            ctx, action="set", project="/some-project", branch=None, state_store=state_store
        )
        msg = ctx.respond.call_args[0][0]
        assert "thread" in msg.lower()
        assert "branch" in msg.lower()

    @pytest.mark.anyio
    async def test_set_in_thread_no_branch_shows_usage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers, "_require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

        thread = DummyThread(parent_id=200, id=10)
        ctx = _make_ctx(channel_id=10)
        ctx.channel = thread
        state_store = _make_state_store()
        await handlers._handle_ctx_command(
            ctx, action="set", project=None, branch=None, state_store=state_store
        )
        msg = ctx.respond.call_args[0][0]
        assert "usage" in msg.lower()

    @pytest.mark.anyio
    async def test_set_in_thread_no_base_context_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers, "_require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

        thread = DummyThread(parent_id=200, id=10)
        ctx = _make_ctx(channel_id=10)
        ctx.channel = thread
        state_store = _make_state_store()
        state_store.get_context = AsyncMock(return_value=None)
        await handlers._handle_ctx_command(
            ctx, action="set", project=None, branch="@feat", state_store=state_store
        )
        msg = ctx.respond.call_args[0][0]
        assert "no project" in msg.lower()

    @pytest.mark.anyio
    async def test_set_in_channel_no_existing_no_project_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers, "_require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

        ctx = _make_ctx(channel_id=100)
        state_store = _make_state_store()
        state_store.get_context = AsyncMock(return_value=None)
        await handlers._handle_ctx_command(
            ctx, action="set", project=None, branch=None, state_store=state_store
        )
        msg = ctx.respond.call_args[0][0]
        assert "no context" in msg.lower()

    @pytest.mark.anyio
    async def test_set_in_channel_uses_existing_project_when_omitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers, "_require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

        existing = DiscordChannelContext(
            project="/myproj",
            worktrees_dir=".wt",
            default_engine="claude",
            worktree_base="main",
        )
        ctx = _make_ctx(channel_id=100)
        state_store = _make_state_store()
        state_store.get_context = AsyncMock(return_value=existing)
        await handlers._handle_ctx_command(
            ctx, action="set", project=None, branch="@dev", state_store=state_store
        )
        state_store.set_context.assert_awaited_once()
        saved = state_store.set_context.call_args[0][2]
        assert saved.project == "/myproj"
        assert saved.worktree_base == "dev"

    @pytest.mark.anyio
    async def test_set_empty_branch_treated_as_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers, "_require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

        existing = DiscordChannelContext(
            project="/proj",
            worktrees_dir=".wt",
            default_engine="claude",
            worktree_base="main",
        )
        ctx = _make_ctx(channel_id=100)
        state_store = _make_state_store()
        state_store.get_context = AsyncMock(return_value=existing)
        await handlers._handle_ctx_command(
            ctx, action="set", project="/proj", branch="   ", state_store=state_store
        )
        saved = state_store.set_context.call_args[0][2]
        # Empty branch means keep existing base
        assert saved.worktree_base == "main"

    @pytest.mark.anyio
    async def test_show_no_context_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

        ctx = _make_ctx(channel_id=100)
        state_store = _make_state_store()
        state_store.get_context = AsyncMock(return_value=None)
        await handlers._handle_ctx_command(
            ctx, action="show", project=None, branch=None, state_store=state_store
        )
        msg = ctx.respond.call_args[0][0]
        assert "no context" in msg.lower()

    @pytest.mark.anyio
    async def test_show_channel_only_no_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

        channel_ctx = DiscordChannelContext(
            project="/proj",
            worktrees_dir=".wt",
            default_engine="claude",
            worktree_base="main",
        )
        ctx = _make_ctx(channel_id=100)
        state_store = _make_state_store()
        state_store.get_context = AsyncMock(return_value=channel_ctx)
        await handlers._handle_ctx_command(
            ctx, action=None, project=None, branch=None, state_store=state_store
        )
        msg = ctx.respond.call_args[0][0]
        assert "**Resolved**" in msg
        assert "`/proj`" in msg
        assert "`main`" in msg
        assert "channel" in msg.lower()
        # No thread section since not in a thread
        assert "Thread:" not in msg

    @pytest.mark.anyio
    async def test_show_in_thread_with_no_thread_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """In a thread with channel context but no thread-specific context."""
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

        channel_ctx = DiscordChannelContext(
            project="/proj",
            worktrees_dir=".wt",
            default_engine="claude",
            worktree_base="main",
        )
        thread = DummyThread(parent_id=200, id=10)
        ctx = _make_ctx(channel_id=10)
        ctx.channel = thread

        async def get_context(_guild_id: int, cid: int):
            if cid == 10:
                return None
            if cid == 200:
                return channel_ctx
            return None

        state_store = _make_state_store()
        state_store.get_context = AsyncMock(side_effect=get_context)
        await handlers._handle_ctx_command(
            ctx, action=None, project=None, branch=None, state_store=state_store
        )
        msg = ctx.respond.call_args[0][0]
        assert "Source: channel" in msg
        assert "Thread: _none_" in msg

    @pytest.mark.anyio
    async def test_show_no_channel_context_shows_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """In a thread with thread context but no channel context."""
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

        thread_ctx = DiscordThreadContext(
            project="/proj",
            branch="feat",
            worktrees_dir=".wt",
            default_engine="claude",
        )
        thread = DummyThread(parent_id=200, id=10)
        ctx = _make_ctx(channel_id=10)
        ctx.channel = thread

        async def get_context(_guild_id: int, cid: int):
            if cid == 10:
                return thread_ctx
            return None

        state_store = _make_state_store()
        state_store.get_context = AsyncMock(side_effect=get_context)
        await handlers._handle_ctx_command(
            ctx, action=None, project=None, branch=None, state_store=state_store
        )
        msg = ctx.respond.call_args[0][0]
        assert "Channel: _none_" in msg
        assert "`feat`" in msg


# ===========================================================================
# _handle_engine_command
# ===========================================================================


class TestHandleEngineCommand:
    @pytest.mark.anyio
    async def test_no_guild_responds_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        ctx = _make_ctx(guild_id=None)
        cfg = MagicMock()
        await handlers._handle_engine_command(
            ctx,
            engine_id="claude",
            prompt="hi",
            cfg=cfg,
            state_store=_make_state_store(),
            prefs_store=_make_prefs_store(),
            running_tasks={},
        )
        msg = ctx.respond.call_args[0][0]
        assert "server" in msg.lower()

    @pytest.mark.anyio
    async def test_user_not_allowed_responds_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(
            handlers, "is_user_allowed", lambda allowed, uid: False
        )
        ctx = _make_ctx()
        ctx.defer = AsyncMock()
        cfg = MagicMock()
        cfg.allowed_user_ids = frozenset({999})
        await handlers._handle_engine_command(
            ctx,
            engine_id="claude",
            prompt="hi",
            cfg=cfg,
            state_store=_make_state_store(),
            prefs_store=_make_prefs_store(),
            running_tasks={},
        )
        msg = ctx.respond.call_args[0][0]
        assert "not allowed" in msg.lower()

    @pytest.mark.anyio
    async def test_creates_thread_in_text_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)
        monkeypatch.setattr(handlers.discord, "TextChannel", DummyTextChannel)
        monkeypatch.setattr(handlers, "is_user_allowed", lambda a, u: True)

        ctx = _make_ctx(channel_id=100)
        ctx.channel = DummyTextChannel()

        from tunapi.transport import MessageRef

        starter = MessageRef(channel_id=555, message_id=777, thread_id=555)

        cfg = MagicMock()
        cfg.exec_cfg = MagicMock()
        cfg.runtime = MagicMock()
        cfg.show_resume_line = True
        cfg.allowed_user_ids = None
        cfg.session_mode = "none"
        cfg.bot.send_message = AsyncMock(return_value=starter)
        cfg.bot.create_thread_without_message = AsyncMock(return_value=555)

        state_store = _make_state_store()
        channel_ctx = DiscordChannelContext(project="/proj", worktree_base="main")

        async def get_context(_gid, cid):
            if cid == 100:
                return channel_ctx
            return None

        state_store.get_context = AsyncMock(side_effect=get_context)

        run_engine_mock = AsyncMock()

        with (
            patch(
                "tunapi.discord.handlers.resolve_overrides",
                new=AsyncMock(return_value=ResolvedOverrides()),
            ),
            patch("tunapi.discord.commands.executor._run_engine", new=run_engine_mock),
        ):
            await handlers._handle_engine_command(
                ctx,
                engine_id="claude",
                prompt="do something",
                cfg=cfg,
                state_store=state_store,
                prefs_store=_make_prefs_store(),
                running_tasks={},
            )
            await asyncio.sleep(0)

        cfg.bot.create_thread_without_message.assert_awaited_once()
        # Thread context should be saved for new thread
        state_store.set_context.assert_awaited_once()
        saved_ctx = state_store.set_context.call_args[0][2]
        assert isinstance(saved_ctx, DiscordThreadContext)
        assert saved_ctx.default_engine == "claude"
        assert saved_ctx.project == "/proj"

    @pytest.mark.anyio
    async def test_thread_creation_fails_runs_in_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)
        monkeypatch.setattr(handlers.discord, "TextChannel", DummyTextChannel)
        monkeypatch.setattr(handlers, "is_user_allowed", lambda a, u: True)

        ctx = _make_ctx(channel_id=100)
        ctx.channel = DummyTextChannel()

        from tunapi.transport import MessageRef

        starter = MessageRef(channel_id=100, message_id=777, thread_id=None)

        cfg = MagicMock()
        cfg.exec_cfg = MagicMock()
        cfg.runtime = MagicMock()
        cfg.show_resume_line = True
        cfg.allowed_user_ids = None
        cfg.session_mode = "none"
        cfg.bot.send_message = AsyncMock(return_value=starter)
        cfg.bot.create_thread_without_message = AsyncMock(return_value=None)  # fails

        state_store = _make_state_store()
        run_engine_mock = AsyncMock()

        with (
            patch(
                "tunapi.discord.handlers.resolve_overrides",
                new=AsyncMock(return_value=ResolvedOverrides()),
            ),
            patch("tunapi.discord.commands.executor._run_engine", new=run_engine_mock),
        ):
            await handlers._handle_engine_command(
                ctx,
                engine_id="claude",
                prompt="hi",
                cfg=cfg,
                state_store=state_store,
                prefs_store=_make_prefs_store(),
                running_tasks={},
            )
            await asyncio.sleep(0)

        # Followup message should note thread creation failed
        followup_msg = ctx.followup.send.call_args[0][0]
        assert "thread creation failed" in followup_msg.lower() or "channel" in followup_msg.lower()

    @pytest.mark.anyio
    async def test_starter_message_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)
        monkeypatch.setattr(handlers.discord, "TextChannel", DummyTextChannel)
        monkeypatch.setattr(handlers, "is_user_allowed", lambda a, u: True)

        ctx = _make_ctx(channel_id=100)
        ctx.channel = DummyTextChannel()

        cfg = MagicMock()
        cfg.exec_cfg = MagicMock()
        cfg.runtime = MagicMock()
        cfg.show_resume_line = True
        cfg.allowed_user_ids = None
        cfg.session_mode = "none"
        cfg.bot.send_message = AsyncMock(return_value=None)  # fails
        cfg.bot.create_thread_without_message = AsyncMock(return_value=None)

        state_store = _make_state_store()

        with patch(
            "tunapi.discord.handlers.resolve_overrides",
            new=AsyncMock(return_value=ResolvedOverrides()),
        ):
            await handlers._handle_engine_command(
                ctx,
                engine_id="claude",
                prompt="hi",
                cfg=cfg,
                state_store=state_store,
                prefs_store=_make_prefs_store(),
                running_tasks={},
            )

        followup_msg = ctx.followup.send.call_args[0][0]
        assert "failed" in followup_msg.lower()

    @pytest.mark.anyio
    async def test_with_thread_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)
        monkeypatch.setattr(handlers, "is_user_allowed", lambda a, u: True)

        thread = DummyThread(parent_id=200, id=555)
        ctx = _make_ctx(channel_id=555)
        ctx.channel = thread

        from tunapi.transport import MessageRef

        starter = MessageRef(channel_id=555, message_id=777, thread_id=555)

        cfg = MagicMock()
        cfg.exec_cfg = MagicMock()
        cfg.runtime = MagicMock()
        cfg.show_resume_line = True
        cfg.allowed_user_ids = None
        cfg.session_mode = "none"
        cfg.bot.send_message = AsyncMock(return_value=starter)

        thread_ctx = DiscordThreadContext(
            project="/proj", branch="feat", default_engine="claude"
        )

        async def get_context(_gid, cid):
            if cid == 555:
                return thread_ctx
            return None

        state_store = _make_state_store()
        state_store.get_context = AsyncMock(side_effect=get_context)

        run_engine_mock = AsyncMock()

        with (
            patch(
                "tunapi.discord.handlers.resolve_overrides",
                new=AsyncMock(return_value=ResolvedOverrides()),
            ),
            patch("tunapi.discord.commands.executor._run_engine", new=run_engine_mock),
        ):
            await handlers._handle_engine_command(
                ctx,
                engine_id="codex",
                prompt="test",
                cfg=cfg,
                state_store=state_store,
                prefs_store=_make_prefs_store(),
                running_tasks={},
            )
            await asyncio.sleep(0)

        run_engine_mock.assert_awaited_once()
        kwargs = run_engine_mock.call_args.kwargs
        assert kwargs["engine_override"] == "codex"
        assert kwargs["context"].project == "/proj"
        assert kwargs["context"].branch == "feat"

    @pytest.mark.anyio
    async def test_with_model_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)
        monkeypatch.setattr(handlers, "is_user_allowed", lambda a, u: True)

        thread = DummyThread(parent_id=200, id=555)
        ctx = _make_ctx(channel_id=555)
        ctx.channel = thread

        from tunapi.transport import MessageRef

        starter = MessageRef(channel_id=555, message_id=777, thread_id=555)

        cfg = MagicMock()
        cfg.exec_cfg = MagicMock()
        cfg.runtime = MagicMock()
        cfg.show_resume_line = True
        cfg.allowed_user_ids = None
        cfg.session_mode = "none"
        cfg.bot.send_message = AsyncMock(return_value=starter)

        state_store = _make_state_store()

        overrides = ResolvedOverrides(model="gpt-4", reasoning="high")
        run_engine_mock = AsyncMock()

        with (
            patch(
                "tunapi.discord.handlers.resolve_overrides",
                new=AsyncMock(return_value=overrides),
            ),
            patch("tunapi.discord.commands.executor._run_engine", new=run_engine_mock),
        ):
            await handlers._handle_engine_command(
                ctx,
                engine_id="codex",
                prompt="test",
                cfg=cfg,
                state_store=state_store,
                prefs_store=_make_prefs_store(),
                running_tasks={},
            )
            await asyncio.sleep(0)

        run_engine_mock.assert_awaited_once()
        kwargs = run_engine_mock.call_args.kwargs
        assert kwargs["run_options"].model == "gpt-4"
        assert kwargs["run_options"].reasoning == "high"

    @pytest.mark.anyio
    async def test_chat_session_mode_restores_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)
        monkeypatch.setattr(handlers, "is_user_allowed", lambda a, u: True)

        thread = DummyThread(parent_id=200, id=555)
        ctx = _make_ctx(channel_id=555, author_id=42)
        ctx.channel = thread

        from tunapi.model import ResumeToken
        from tunapi.transport import MessageRef

        starter = MessageRef(channel_id=555, message_id=777, thread_id=555)

        cfg = MagicMock()
        cfg.exec_cfg = MagicMock()
        cfg.runtime = MagicMock()
        cfg.show_resume_line = True
        cfg.allowed_user_ids = None
        cfg.session_mode = "chat"
        cfg.bot.send_message = AsyncMock(return_value=starter)

        state_store = _make_state_store()
        state_store.get_session = AsyncMock(return_value="resume-tok")

        run_engine_mock = AsyncMock()

        with (
            patch(
                "tunapi.discord.handlers.resolve_overrides",
                new=AsyncMock(return_value=ResolvedOverrides()),
            ),
            patch("tunapi.discord.commands.executor._run_engine", new=run_engine_mock),
        ):
            await handlers._handle_engine_command(
                ctx,
                engine_id="claude",
                prompt="test",
                cfg=cfg,
                state_store=state_store,
                prefs_store=_make_prefs_store(),
                running_tasks={},
            )
            await asyncio.sleep(0)

        state_store.get_session.assert_awaited_once_with(1, 555, "claude", author_id=42)
        kwargs = run_engine_mock.call_args.kwargs
        assert kwargs["resume_token"] == ResumeToken(engine="claude", value="resume-tok")

    @pytest.mark.anyio
    async def test_non_chat_session_mode_no_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)
        monkeypatch.setattr(handlers, "is_user_allowed", lambda a, u: True)

        thread = DummyThread(parent_id=200, id=555)
        ctx = _make_ctx(channel_id=555)
        ctx.channel = thread

        from tunapi.transport import MessageRef

        starter = MessageRef(channel_id=555, message_id=777, thread_id=555)

        cfg = MagicMock()
        cfg.exec_cfg = MagicMock()
        cfg.runtime = MagicMock()
        cfg.show_resume_line = True
        cfg.allowed_user_ids = None
        cfg.session_mode = "none"
        cfg.bot.send_message = AsyncMock(return_value=starter)

        state_store = _make_state_store()
        run_engine_mock = AsyncMock()

        with (
            patch(
                "tunapi.discord.handlers.resolve_overrides",
                new=AsyncMock(return_value=ResolvedOverrides()),
            ),
            patch("tunapi.discord.commands.executor._run_engine", new=run_engine_mock),
        ):
            await handlers._handle_engine_command(
                ctx,
                engine_id="claude",
                prompt="test",
                cfg=cfg,
                state_store=state_store,
                prefs_store=_make_prefs_store(),
                running_tasks={},
            )
            await asyncio.sleep(0)

        state_store.get_session.assert_not_awaited()
        kwargs = run_engine_mock.call_args.kwargs
        assert kwargs["resume_token"] is None

    @pytest.mark.anyio
    async def test_author_id_not_int_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When author.id is not an int, author_id should be None."""
        import tunapi.discord.handlers as handlers

        monkeypatch.setattr(handlers.discord, "Thread", DummyThread)
        monkeypatch.setattr(handlers, "is_user_allowed", lambda a, u: True)

        thread = DummyThread(parent_id=200, id=555)
        ctx = _make_ctx(channel_id=555, author_id=None)
        # Make author.id return a non-int
        ctx.author = MagicMock()
        ctx.author.id = "not-an-int"

        from tunapi.transport import MessageRef

        starter = MessageRef(channel_id=555, message_id=777, thread_id=555)

        cfg = MagicMock()
        cfg.exec_cfg = MagicMock()
        cfg.runtime = MagicMock()
        cfg.show_resume_line = True
        cfg.allowed_user_ids = None
        cfg.session_mode = "chat"
        cfg.bot.send_message = AsyncMock(return_value=starter)

        state_store = _make_state_store()
        state_store.get_session = AsyncMock(return_value=None)
        run_engine_mock = AsyncMock()

        with (
            patch(
                "tunapi.discord.handlers.resolve_overrides",
                new=AsyncMock(return_value=ResolvedOverrides()),
            ),
            patch("tunapi.discord.commands.executor._run_engine", new=run_engine_mock),
        ):
            await handlers._handle_engine_command(
                ctx,
                engine_id="claude",
                prompt="test",
                cfg=cfg,
                state_store=state_store,
                prefs_store=_make_prefs_store(),
                running_tasks={},
            )
            await asyncio.sleep(0)

        # author_id should be None since "not-an-int" is not int
        state_store.get_session.assert_awaited_once_with(1, 555, "claude", author_id=None)


# ===========================================================================
# register_engine_commands
# ===========================================================================


class TestRegisterEngineCommands:
    def test_registers_commands_for_available_engines(self) -> None:
        import tunapi.discord.handlers as handlers

        bot = MagicMock()
        bot.bot = MagicMock()
        bot.bot.slash_command = MagicMock(side_effect=lambda **kw: lambda f: f)

        cfg = MagicMock()
        cfg.runtime.available_engine_ids = MagicMock(return_value=["claude", "codex"])

        result = handlers.register_engine_commands(
            bot,
            cfg=cfg,
            state_store=_make_state_store(),
            prefs_store=_make_prefs_store(),
            running_tasks={},
        )
        assert "claude" in result
        assert "codex" in result
        assert len(result) == 2

    def test_empty_engines_returns_empty(self) -> None:
        import tunapi.discord.handlers as handlers

        bot = MagicMock()
        bot.bot = MagicMock()

        cfg = MagicMock()
        cfg.runtime.available_engine_ids = MagicMock(return_value=[])

        result = handlers.register_engine_commands(
            bot,
            cfg=cfg,
            state_store=_make_state_store(),
            prefs_store=_make_prefs_store(),
            running_tasks={},
        )
        assert result == []


# ===========================================================================
# _format_engine_starter_message
# ===========================================================================


class TestFormatEngineStarterMessage:
    def test_fits_within_limit(self) -> None:
        from tunapi.discord.handlers import _format_engine_starter_message

        result = _format_engine_starter_message("claude", "hello", max_chars=2000)
        assert result == "/claude hello"

    def test_truncated(self) -> None:
        from tunapi.discord.handlers import _format_engine_starter_message

        result = _format_engine_starter_message("claude", "x" * 200, max_chars=20)
        assert len(result) <= 20
        assert result.endswith("\u2026")  # ellipsis

    def test_exact_boundary(self) -> None:
        from tunapi.discord.handlers import _format_engine_starter_message

        prompt = "a" * 10
        result = _format_engine_starter_message("x", prompt, max_chars=len("/x ") + 10)
        assert result == "/x " + "a" * 10


# ===========================================================================
# register_slash_commands — inner command handlers
# ===========================================================================


class TestRegisterSlashCommands:
    """Test by calling the registered inner handlers directly."""

    def _register_and_capture(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        state_store=None,
        prefs_store=None,
        runtime=None,
        allowed_user_ids=None,
        trigger_mode_default="all",
    ):
        """Register slash commands and capture registered handlers by name."""
        import tunapi.discord.handlers as handlers

        captured = {}

        class FakeBot:
            def slash_command(self, **kwargs):
                name = kwargs["name"]
                def decorator(func):
                    captured[name] = func
                    return func
                return decorator

        bot = MagicMock()
        bot.bot = FakeBot()

        handlers.register_slash_commands(
            bot,
            state_store=state_store or _make_state_store(),
            prefs_store=prefs_store or _make_prefs_store(),
            get_running_task=lambda ch: None,
            cancel_task=AsyncMock(),
            allowed_user_ids=allowed_user_ids,
            trigger_mode_default=trigger_mode_default,
            runtime=runtime,
        )
        return captured

    @pytest.mark.anyio
    async def test_status_no_guild(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        cmds = self._register_and_capture(monkeypatch)
        ctx = _make_ctx(guild_id=None)
        await cmds["status"](ctx)
        msg = ctx.respond.call_args[0][0]
        assert "server" in msg.lower()

    @pytest.mark.anyio
    async def test_status_no_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        state_store = _make_state_store()
        cmds = self._register_and_capture(monkeypatch, state_store=state_store)
        ctx = _make_ctx()
        await cmds["status"](ctx)
        msg = ctx.respond.call_args[0][0]
        assert "no context" in msg.lower()

    @pytest.mark.anyio
    async def test_status_with_channel_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        channel_ctx = DiscordChannelContext(
            project="/proj", worktree_base="main", default_engine="claude"
        )
        state_store = _make_state_store()
        state_store.get_context = AsyncMock(return_value=channel_ctx)
        cmds = self._register_and_capture(monkeypatch, state_store=state_store)

        ctx = _make_ctx(channel_id=100)
        await cmds["status"](ctx)
        msg = ctx.respond.call_args[0][0]
        assert "Channel Status" in msg
        assert "`/proj`" in msg
        assert "idle" in msg

    @pytest.mark.anyio
    async def test_status_with_thread_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        thread_ctx = DiscordThreadContext(
            project="/proj", branch="feat", default_engine="codex"
        )
        state_store = _make_state_store()
        state_store.get_context = AsyncMock(return_value=thread_ctx)
        cmds = self._register_and_capture(monkeypatch, state_store=state_store)

        ctx = _make_ctx(channel_id=100)
        await cmds["status"](ctx)
        msg = ctx.respond.call_args[0][0]
        assert "Thread Status" in msg
        assert "`feat`" in msg

    @pytest.mark.anyio
    async def test_status_with_running_task(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)

        channel_ctx = DiscordChannelContext(project="/proj", worktree_base="main")
        state_store = _make_state_store()
        state_store.get_context = AsyncMock(return_value=channel_ctx)

        import tunapi.discord.handlers as handlers

        captured = {}

        class FakeBot:
            def slash_command(self, **kwargs):
                name = kwargs["name"]
                def decorator(func):
                    captured[name] = func
                    return func
                return decorator

        bot = MagicMock()
        bot.bot = FakeBot()

        handlers.register_slash_commands(
            bot,
            state_store=state_store,
            prefs_store=_make_prefs_store(),
            get_running_task=lambda ch: 12345,
            cancel_task=AsyncMock(),
            runtime=None,
        )

        ctx = _make_ctx(channel_id=100)
        await captured["status"](ctx)
        msg = ctx.respond.call_args[0][0]
        assert "running" in msg.lower()
        assert "12345" in msg

    @pytest.mark.anyio
    async def test_bind_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        state_store = _make_state_store()
        cmds = self._register_and_capture(monkeypatch, state_store=state_store)
        ctx = _make_ctx()
        await cmds["bind"](ctx, project="~/dev/proj", worktrees_dir=".wt", default_engine="claude", worktree_base="main")
        state_store.set_context.assert_awaited_once()
        msg = ctx.respond.call_args[0][0]
        assert "~/dev/proj" in msg

    @pytest.mark.anyio
    async def test_bind_no_guild(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        cmds = self._register_and_capture(monkeypatch)
        ctx = _make_ctx(guild_id=None)
        await cmds["bind"](ctx, project="~/proj", worktrees_dir=".wt", default_engine="claude", worktree_base="main")
        msg = ctx.respond.call_args[0][0]
        assert "server" in msg.lower()

    @pytest.mark.anyio
    async def test_unbind_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        state_store = _make_state_store()
        prefs_store = _make_prefs_store()
        cmds = self._register_and_capture(monkeypatch, state_store=state_store, prefs_store=prefs_store)
        ctx = _make_ctx()
        await cmds["unbind"](ctx)
        state_store.clear_channel.assert_awaited_once()
        prefs_store.clear_channel.assert_awaited_once()

    @pytest.mark.anyio
    async def test_cancel_no_running_task(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        cmds = self._register_and_capture(monkeypatch)
        ctx = _make_ctx()
        await cmds["cancel"](ctx)
        msg = ctx.respond.call_args[0][0]
        assert "no task" in msg.lower()

    @pytest.mark.anyio
    async def test_cancel_with_running_task(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)

        import tunapi.discord.handlers as handlers

        captured = {}
        cancel_mock = AsyncMock()

        class FakeBot:
            def slash_command(self, **kwargs):
                name = kwargs["name"]
                def decorator(func):
                    captured[name] = func
                    return func
                return decorator

        bot = MagicMock()
        bot.bot = FakeBot()

        handlers.register_slash_commands(
            bot,
            state_store=_make_state_store(),
            prefs_store=_make_prefs_store(),
            get_running_task=lambda ch: 999,
            cancel_task=cancel_mock,
            runtime=None,
        )

        ctx = _make_ctx()
        await captured["cancel"](ctx)
        cancel_mock.assert_awaited_once()
        msg = ctx.respond.call_args[0][0]
        assert "cancellation" in msg.lower()

    @pytest.mark.anyio
    async def test_new_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        state_store = _make_state_store()
        cmds = self._register_and_capture(monkeypatch, state_store=state_store)
        ctx = _make_ctx(author_id=42)
        await cmds["new"](ctx)
        state_store.clear_sessions.assert_awaited_once_with(1, 100, author_id=42)

    @pytest.mark.anyio
    async def test_user_not_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: False)
        cmds = self._register_and_capture(monkeypatch, allowed_user_ids=frozenset({999}))
        ctx = _make_ctx(author_id=1)
        await cmds["status"](ctx)
        msg = ctx.respond.call_args[0][0]
        assert "not allowed" in msg.lower()

    @pytest.mark.anyio
    async def test_agent_command_show_no_runtime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        cmds = self._register_and_capture(monkeypatch, runtime=None)
        ctx = _make_ctx()
        await cmds["agent"](ctx, action=None, engine=None)
        msg = ctx.respond.call_args[0][0]
        assert "runtime" in msg.lower()

    @pytest.mark.anyio
    async def test_agent_command_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers._require_admin", AsyncMock(return_value=True))

        runtime = MagicMock()
        runtime.engine_ids = ["claude", "codex"]
        prefs_store = _make_prefs_store()
        cmds = self._register_and_capture(monkeypatch, runtime=runtime, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["agent"](ctx, action="set", engine="codex")
        prefs_store.set_default_engine.assert_awaited_once()
        msg = ctx.respond.call_args[0][0]
        assert "codex" in msg

    @pytest.mark.anyio
    async def test_agent_command_set_unknown_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers._require_admin", AsyncMock(return_value=True))

        runtime = MagicMock()
        runtime.engine_ids = ["claude", "codex"]
        cmds = self._register_and_capture(monkeypatch, runtime=runtime)

        ctx = _make_ctx()
        await cmds["agent"](ctx, action="set", engine="unknown")
        msg = ctx.respond.call_args[0][0]
        assert "unknown engine" in msg.lower()

    @pytest.mark.anyio
    async def test_agent_command_set_no_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers._require_admin", AsyncMock(return_value=True))

        runtime = MagicMock()
        runtime.engine_ids = ["claude"]
        cmds = self._register_and_capture(monkeypatch, runtime=runtime)

        ctx = _make_ctx()
        await cmds["agent"](ctx, action="set", engine=None)
        msg = ctx.respond.call_args[0][0]
        assert "missing" in msg.lower()

    @pytest.mark.anyio
    async def test_agent_command_clear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers._require_admin", AsyncMock(return_value=True))

        runtime = MagicMock()
        runtime.engine_ids = ["claude"]
        prefs_store = _make_prefs_store()
        cmds = self._register_and_capture(monkeypatch, runtime=runtime, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["agent"](ctx, action="clear", engine=None)
        prefs_store.set_default_engine.assert_awaited_once()
        msg = ctx.respond.call_args[0][0]
        assert "cleared" in msg.lower()

    @pytest.mark.anyio
    async def test_agent_command_show(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr(
            "tunapi.discord.handlers.resolve_effective_default_engine",
            AsyncMock(return_value=("claude", "config")),
        )
        monkeypatch.setattr(
            "tunapi.discord.handlers.resolve_overrides",
            AsyncMock(return_value=ResolvedOverrides()),
        )
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        runtime = MagicMock()
        runtime.engine_ids = ["claude", "codex"]
        runtime.default_engine = "claude"
        state_store = _make_state_store()
        prefs_store = _make_prefs_store()
        cmds = self._register_and_capture(
            monkeypatch, runtime=runtime, state_store=state_store, prefs_store=prefs_store
        )

        ctx = _make_ctx()
        await cmds["agent"](ctx, action=None, engine=None)
        msg = ctx.respond.call_args[0][0]
        assert "Available Agents" in msg
        assert "`claude`" in msg

    @pytest.mark.anyio
    async def test_agent_command_show_no_engines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        runtime = MagicMock()
        runtime.engine_ids = []
        cmds = self._register_and_capture(monkeypatch, runtime=runtime)

        ctx = _make_ctx()
        await cmds["agent"](ctx, action=None, engine=None)
        msg = ctx.respond.call_args[0][0]
        assert "no engines" in msg.lower()

    @pytest.mark.anyio
    async def test_agent_command_show_with_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr(
            "tunapi.discord.handlers.resolve_effective_default_engine",
            AsyncMock(return_value=("claude", "channel_override")),
        )
        monkeypatch.setattr(
            "tunapi.discord.handlers.resolve_overrides",
            AsyncMock(
                return_value=ResolvedOverrides(
                    model="gpt-4", source_model="channel", reasoning="high", source_reasoning="thread"
                )
            ),
        )
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        runtime = MagicMock()
        runtime.engine_ids = ["claude"]
        runtime.default_engine = "claude"
        state_store = _make_state_store()
        prefs_store = _make_prefs_store()
        cmds = self._register_and_capture(
            monkeypatch, runtime=runtime, state_store=state_store, prefs_store=prefs_store
        )

        ctx = _make_ctx()
        await cmds["agent"](ctx, action=None, engine=None)
        msg = ctx.respond.call_args[0][0]
        assert "**Overrides**" in msg
        assert "`gpt-4`" in msg
        assert "`high`" in msg

    @pytest.mark.anyio
    async def test_model_command_show_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        prefs_store = _make_prefs_store()
        prefs_store.get_all_overrides = AsyncMock(
            return_value=({"claude": "gpt-4"}, {}, None, None)
        )
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["model"](ctx, engine=None, model=None)
        msg = ctx.respond.call_args[0][0]
        assert "Model Overrides" in msg
        assert "`claude`" in msg

    @pytest.mark.anyio
    async def test_model_command_show_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        prefs_store = _make_prefs_store()
        prefs_store.get_all_overrides = AsyncMock(return_value=({}, {}, None, None))
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["model"](ctx, engine=None, model=None)
        msg = ctx.respond.call_args[0][0]
        assert "no model" in msg.lower()

    @pytest.mark.anyio
    async def test_model_command_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers._require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        prefs_store = _make_prefs_store()
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["model"](ctx, engine="claude", model="gpt-4")
        prefs_store.set_model_override.assert_awaited_once()

    @pytest.mark.anyio
    async def test_model_command_clear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers._require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        prefs_store = _make_prefs_store()
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["model"](ctx, engine="claude", model="clear")
        prefs_store.set_model_override.assert_awaited_once_with(1, 100, "claude", None)

    @pytest.mark.anyio
    async def test_model_command_show_specific_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        prefs_store = _make_prefs_store()
        prefs_store.get_model_override = AsyncMock(return_value="gpt-4")
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["model"](ctx, engine="claude", model=None)
        msg = ctx.respond.call_args[0][0]
        assert "gpt-4" in msg

    @pytest.mark.anyio
    async def test_model_command_show_specific_engine_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        prefs_store = _make_prefs_store()
        prefs_store.get_model_override = AsyncMock(return_value=None)
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["model"](ctx, engine="claude", model=None)
        msg = ctx.respond.call_args[0][0]
        assert "no model override" in msg.lower()

    @pytest.mark.anyio
    async def test_reasoning_command_show_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        prefs_store = _make_prefs_store()
        prefs_store.get_all_overrides = AsyncMock(
            return_value=({}, {"codex": "high"}, None, None)
        )
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["reasoning"](ctx, engine=None, level=None)
        msg = ctx.respond.call_args[0][0]
        assert "Reasoning Overrides" in msg
        assert "`high`" in msg

    @pytest.mark.anyio
    async def test_reasoning_command_show_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        prefs_store = _make_prefs_store()
        prefs_store.get_all_overrides = AsyncMock(return_value=({}, {}, None, None))
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["reasoning"](ctx, engine=None, level=None)
        msg = ctx.respond.call_args[0][0]
        assert "no reasoning" in msg.lower()

    @pytest.mark.anyio
    async def test_reasoning_command_set_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers._require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)
        monkeypatch.setattr("tunapi.discord.handlers.is_valid_reasoning_level", lambda l: True)
        monkeypatch.setattr("tunapi.discord.handlers.supports_reasoning", lambda e: True)

        prefs_store = _make_prefs_store()
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["reasoning"](ctx, engine="codex", level="High")
        prefs_store.set_reasoning_override.assert_awaited_once_with(1, 100, "codex", "high")

    @pytest.mark.anyio
    async def test_reasoning_command_set_invalid_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers._require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)
        monkeypatch.setattr("tunapi.discord.handlers.is_valid_reasoning_level", lambda l: False)

        cmds = self._register_and_capture(monkeypatch)

        ctx = _make_ctx()
        await cmds["reasoning"](ctx, engine="codex", level="extreme")
        msg = ctx.respond.call_args[0][0]
        assert "invalid" in msg.lower()

    @pytest.mark.anyio
    async def test_reasoning_command_unsupported_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers._require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)
        monkeypatch.setattr("tunapi.discord.handlers.is_valid_reasoning_level", lambda l: True)
        monkeypatch.setattr("tunapi.discord.handlers.supports_reasoning", lambda e: False)

        cmds = self._register_and_capture(monkeypatch)

        ctx = _make_ctx()
        await cmds["reasoning"](ctx, engine="claude", level="high")
        msg = ctx.respond.call_args[0][0]
        assert "does not support" in msg.lower()

    @pytest.mark.anyio
    async def test_reasoning_command_clear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers._require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        prefs_store = _make_prefs_store()
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["reasoning"](ctx, engine="codex", level="clear")
        prefs_store.set_reasoning_override.assert_awaited_once_with(1, 100, "codex", None)

    @pytest.mark.anyio
    async def test_reasoning_command_show_specific(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        prefs_store = _make_prefs_store()
        prefs_store.get_reasoning_override = AsyncMock(return_value="medium")
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["reasoning"](ctx, engine="codex", level=None)
        msg = ctx.respond.call_args[0][0]
        assert "medium" in msg

    @pytest.mark.anyio
    async def test_reasoning_command_show_specific_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        prefs_store = _make_prefs_store()
        prefs_store.get_reasoning_override = AsyncMock(return_value=None)
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["reasoning"](ctx, engine="codex", level=None)
        msg = ctx.respond.call_args[0][0]
        assert "no reasoning override" in msg.lower()

    @pytest.mark.anyio
    async def test_trigger_command_show(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)
        monkeypatch.setattr(
            "tunapi.discord.handlers.resolve_trigger_mode",
            AsyncMock(return_value="all"),
        )

        prefs_store = _make_prefs_store()
        prefs_store.get_trigger_mode = AsyncMock(return_value="all")
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["trigger"](ctx, mode=None)
        msg = ctx.respond.call_args[0][0]
        assert "`all`" in msg
        assert "set on this" in msg.lower()

    @pytest.mark.anyio
    async def test_trigger_command_show_inherited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)
        monkeypatch.setattr(
            "tunapi.discord.handlers.resolve_trigger_mode",
            AsyncMock(return_value="all"),
        )

        prefs_store = _make_prefs_store()
        prefs_store.get_trigger_mode = AsyncMock(return_value=None)
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["trigger"](ctx, mode=None)
        msg = ctx.respond.call_args[0][0]
        assert "inherited" in msg.lower()

    @pytest.mark.anyio
    async def test_trigger_command_set_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers._require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        prefs_store = _make_prefs_store()
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["trigger"](ctx, mode="all")
        prefs_store.set_trigger_mode.assert_awaited_once_with(1, 100, "all")
        msg = ctx.respond.call_args[0][0]
        assert "all messages" in msg.lower() or "`all`" in msg

    @pytest.mark.anyio
    async def test_trigger_command_set_mentions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers._require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        prefs_store = _make_prefs_store()
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["trigger"](ctx, mode="mentions")
        prefs_store.set_trigger_mode.assert_awaited_once_with(1, 100, "mentions")
        msg = ctx.respond.call_args[0][0]
        assert "@mentioned" in msg.lower() or "`mentions`" in msg

    @pytest.mark.anyio
    async def test_trigger_command_clear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers._require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        prefs_store = _make_prefs_store()
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx()
        await cmds["trigger"](ctx, mode="clear")
        prefs_store.set_trigger_mode.assert_awaited_once_with(1, 100, None)

    @pytest.mark.anyio
    async def test_ctx_command_delegates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)

        handle_ctx = AsyncMock()
        monkeypatch.setattr("tunapi.discord.handlers._handle_ctx_command", handle_ctx)

        state_store = _make_state_store()
        cmds = self._register_and_capture(monkeypatch, state_store=state_store)

        ctx = _make_ctx()
        await cmds["ctx"](ctx, action="show", project=None, branch=None)
        handle_ctx.assert_awaited_once()

    @pytest.mark.anyio
    async def test_model_command_in_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Model override stored on thread_id when in thread."""
        monkeypatch.setattr("tunapi.discord.handlers.is_user_allowed", lambda a, u: True)
        monkeypatch.setattr("tunapi.discord.handlers._require_admin", AsyncMock(return_value=True))
        monkeypatch.setattr("tunapi.discord.handlers.discord.Thread", DummyThread)

        prefs_store = _make_prefs_store()
        cmds = self._register_and_capture(monkeypatch, prefs_store=prefs_store)

        ctx = _make_ctx(channel_id=10)
        ctx.channel = DummyThread(parent_id=200, id=10)
        await cmds["model"](ctx, engine="claude", model="gpt-4")
        # Should store on thread_id=10
        prefs_store.set_model_override.assert_awaited_once_with(1, 10, "claude", "gpt-4")
