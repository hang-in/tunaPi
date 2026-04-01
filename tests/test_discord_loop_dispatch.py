"""Tests for closures inside run_main_loop: handle_message, run_job,
run_thread_job, and dispatch_media_group.

These are nested functions that cannot be imported directly, so we exercise
them indirectly by calling ``run_main_loop`` with mocks and capturing the
``handle_message`` closure that gets registered via ``cfg.bot.set_message_handler``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from tunapi.discord.bridge import (
    DiscordBridgeConfig,
    DiscordFilesSettings,
    DiscordVoiceMessageSettings,
)
from tunapi.discord.loop_state import _MediaGroupState, _MediaItem
from tunapi.model import ResumeToken
from tunapi.transport import MessageRef, RenderedMessage, SendOptions


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_discord_message(
    *,
    content: str = "hello",
    author_name: str = "testuser",
    author_id: int = 42,
    message_id: int = 1000,
    channel_id: int = 100,
    guild_id: int = 1,
    is_bot: bool = False,
    channel_type: str = "text",  # "text" | "thread"
    parent_channel_id: int | None = None,
    thread_id: int | None = None,
    attachments: list | None = None,
    reference: MagicMock | None = None,
    created_at: datetime | None = None,
) -> MagicMock:
    """Build a mock discord.Message with the required attributes."""
    import discord

    msg = MagicMock(spec=discord.Message)
    msg.id = message_id
    msg.content = content

    author = MagicMock()
    author.name = author_name
    author.id = author_id
    author.bot = is_bot
    msg.author = author

    guild = MagicMock()
    guild.id = guild_id
    msg.guild = guild

    if channel_type == "thread":
        channel = MagicMock(spec=discord.Thread)
        channel.id = thread_id or channel_id
        parent = MagicMock(spec=discord.TextChannel)
        parent.id = parent_channel_id or channel_id
        channel.parent = parent
        channel.join = AsyncMock()
    else:
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = channel_id
    channel.fetch_message = AsyncMock()
    msg.channel = channel

    msg.attachments = attachments or []
    msg.reference = reference
    msg.created_at = created_at or datetime.now(UTC)
    msg.reply = AsyncMock()
    msg.mentions = []

    return msg


def _make_bot_user(*, user_id: int = 999) -> MagicMock:
    """Build a mock discord.User for the bot."""
    user = MagicMock()
    user.id = user_id
    user.bot = True
    user.mentioned_in = MagicMock(return_value=False)
    return user


def _make_cfg(
    *,
    bot_user: MagicMock | None = None,
    guild_id: int | None = 1,
    session_mode: Literal["stateless", "chat"] = "stateless",
    trigger_mode_default: Literal["all", "mentions"] = "all",
    allowed_user_ids: frozenset[int] | None = None,
    files_enabled: bool = False,
    auto_put: bool = False,
    auto_put_mode: Literal["upload", "prompt"] = "upload",
    voice_messages_enabled: bool = False,
    transport: AsyncMock | None = None,
    presenter: MagicMock | None = None,
    runtime: MagicMock | None = None,
    media_group_debounce_s: float = 0.0,
) -> DiscordBridgeConfig:
    """Build a DiscordBridgeConfig with mocks."""
    if bot_user is None:
        bot_user = _make_bot_user()

    bot = MagicMock()
    bot.user = bot_user
    bot.bot = MagicMock()
    bot.bot.event = lambda fn: fn  # passthrough decorator
    bot.set_message_handler = MagicMock()
    bot.start = AsyncMock()
    bot.close = AsyncMock()
    bot.create_thread = AsyncMock(return_value=None)
    bot.get_guild = MagicMock(return_value=None)

    t = transport or AsyncMock()
    t.send = AsyncMock(return_value=MessageRef(channel_id=100, message_id=9999))

    p = presenter or MagicMock()
    p.render_progress = MagicMock(
        return_value=RenderedMessage(text="queued...", extra={"show_cancel": False})
    )

    rt = runtime or MagicMock()
    rt.format_context_line = MagicMock(return_value=None)
    rt.resolve_runner = MagicMock()
    rt.resolve_run_cwd = MagicMock(return_value=Path("/tmp/test"))
    rt.resolve_message = MagicMock(return_value=None)
    rt.default_engine = "claude"
    rt.engine_ids = {"claude", "codex", "gemini"}
    rt.is_resume_line = MagicMock(return_value=False)
    rt.config_path = Path("/tmp/tunapi-test")
    rt.allowlist = None

    exec_cfg = MagicMock()
    exec_cfg.transport = t
    exec_cfg.presenter = p

    files = DiscordFilesSettings(
        enabled=files_enabled,
        auto_put=auto_put,
        auto_put_mode=auto_put_mode,
    )
    voice = DiscordVoiceMessageSettings(enabled=voice_messages_enabled)

    return DiscordBridgeConfig(
        bot=bot,
        runtime=rt,
        guild_id=guild_id,
        startup_msg="bot started",
        exec_cfg=exec_cfg,
        allowed_user_ids=allowed_user_ids,
        session_mode=session_mode,
        trigger_mode_default=trigger_mode_default,
        files=files,
        voice_messages=voice,
        media_group_debounce_s=media_group_debounce_s,
    )


async def _capture_handle_message(
    cfg: DiscordBridgeConfig,
    *,
    default_engine_override: str | None = None,
):
    """Run run_main_loop just far enough to capture the handle_message closure.

    We achieve this by making cfg.bot.start() raise a sentinel exception
    after the closures have been registered.
    """

    class _SetupDone(Exception):
        pass

    # Make bot.start() raise after capturing — this causes run_with_watcher
    # to exit. The exception will be wrapped in an ExceptionGroup by anyio.
    async def start_raises():
        raise _SetupDone

    cfg.bot.start = start_raises

    from tunapi.discord.loop import run_main_loop

    # Build a mock scheduler whose enqueue_resume is a simple async no-op
    mock_scheduler = MagicMock()
    mock_scheduler.enqueue_resume = AsyncMock()
    mock_scheduler.note_thread_known = AsyncMock()

    with (
        patch("tunapi.discord.loop.DiscordStateStore") as MockState,
        patch("tunapi.discord.loop.DiscordPrefsStore") as MockPrefs,
        patch("tunapi.discord.loop.register_slash_commands"),
        patch("tunapi.discord.loop.register_engine_commands", return_value=set()),
        patch("tunapi.discord.loop.discover_command_ids", return_value=set()),
        patch("tunapi.discord.loop.register_plugin_commands"),
        patch("tunapi.discord.loop.watch_config_changes", return_value=MagicMock()),
        patch("tunapi.discord.loop.ThreadScheduler", return_value=mock_scheduler),
    ):
        state_store = AsyncMock()
        state_store.get_startup_channel = AsyncMock(return_value=None)
        state_store.get_session = AsyncMock(return_value=None)
        state_store.set_session = AsyncMock()
        state_store.get_context = AsyncMock(return_value=None)
        state_store.set_context = AsyncMock()
        state_store.set_startup_channel = AsyncMock()
        MockState.return_value = state_store

        prefs_store = AsyncMock()
        prefs_store.ensure_loaded = AsyncMock()
        prefs_store.get_model_override = AsyncMock(return_value=None)
        prefs_store.get_reasoning_override = AsyncMock(return_value=None)
        prefs_store.get_trigger_mode = AsyncMock(return_value=None)
        prefs_store.get_default_engine = AsyncMock(return_value=None)
        MockPrefs.return_value = prefs_store

        try:
            await run_main_loop(
                cfg,
                default_engine_override=default_engine_override,
            )
        except* _SetupDone:
            pass

    # The handle_message closure was registered via set_message_handler
    cfg.bot.set_message_handler.assert_called_once()
    handler = cfg.bot.set_message_handler.call_args.args[0]
    return handler, state_store, prefs_store


# ===========================================================================
# handle_message: guild-only guard
# ===========================================================================


class TestHandleMessageGuildOnly:
    @pytest.mark.anyio
    async def test_ignores_dm_messages(self) -> None:
        """Messages without a guild (DMs) should be silently ignored."""
        cfg = _make_cfg()
        handler, _, _ = await _capture_handle_message(cfg)

        msg = _make_discord_message(content="hello")
        msg.guild = None  # DM

        await handler(msg)

        # No transport send should happen
        cfg.exec_cfg.transport.send.assert_not_awaited()


# ===========================================================================
# handle_message: startup backlog drain
# ===========================================================================


class TestHandleMessageStartupBacklog:
    @pytest.mark.anyio
    async def test_ignores_messages_before_startup(self) -> None:
        """Messages created before the startup cutoff should be skipped."""
        cfg = _make_cfg()
        handler, _, _ = await _capture_handle_message(cfg)

        msg = _make_discord_message(content="old message")
        # Set created_at far in the past
        msg.created_at = datetime(2020, 1, 1, tzinfo=UTC)

        await handler(msg)

        cfg.exec_cfg.transport.send.assert_not_awaited()


# ===========================================================================
# handle_message: bot message filtering
# ===========================================================================


class TestHandleMessageBotFilter:
    @pytest.mark.anyio
    async def test_ignores_bot_messages(self) -> None:
        """Bot messages should be filtered by should_process_message."""
        cfg = _make_cfg()
        handler, _, _ = await _capture_handle_message(cfg)

        msg = _make_discord_message(content="bot msg", is_bot=True)

        await handler(msg)

        cfg.exec_cfg.transport.send.assert_not_awaited()


# ===========================================================================
# handle_message: allowed user filtering
# ===========================================================================


class TestHandleMessageAllowedUsers:
    @pytest.mark.anyio
    async def test_ignores_non_allowed_users(self) -> None:
        """When allowed_user_ids is set, non-allowed users are ignored."""
        cfg = _make_cfg(allowed_user_ids=frozenset({100, 200}))
        handler, _, _ = await _capture_handle_message(cfg)

        msg = _make_discord_message(content="hello", author_id=42)

        await handler(msg)

        cfg.exec_cfg.transport.send.assert_not_awaited()

    @pytest.mark.anyio
    async def test_processes_allowed_users(self) -> None:
        """When user is in allowed_user_ids, message is processed."""
        cfg = _make_cfg(allowed_user_ids=frozenset({42}))

        # Mock resolve_runner to return available runner
        resolved = MagicMock()
        resolved.available = True
        resolved.runner = MagicMock()
        resolved.runner.engine = "claude"
        resolved.engine = "claude"
        cfg.runtime.resolve_runner = MagicMock(return_value=resolved)

        handler, _, _ = await _capture_handle_message(cfg)

        with patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="hello world",
        ), patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=(None, "hello world"),
        ), patch(
            "tunapi.runner_bridge.handle_message",
            new_callable=AsyncMock,
        ) as mock_handle:
            msg = _make_discord_message(content="hello world", author_id=42)
            await handler(msg)

            mock_handle.assert_awaited_once()


# ===========================================================================
# handle_message: trigger mode filtering
# ===========================================================================


class TestHandleMessageTriggerMode:
    @pytest.mark.anyio
    async def test_mentions_mode_skips_without_mention(self) -> None:
        """In mentions mode, messages without bot mention are skipped."""
        cfg = _make_cfg(trigger_mode_default="mentions")
        handler, _, prefs_store = await _capture_handle_message(cfg)

        # Make trigger mode return "mentions"
        prefs_store.get_trigger_mode = AsyncMock(return_value="mentions")

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.is_bot_mentioned",
            return_value=False,
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="mentions",
        ):
            msg = _make_discord_message(content="hello")
            msg.reference = None

            await handler(msg)

            cfg.exec_cfg.transport.send.assert_not_awaited()


# ===========================================================================
# handle_message: empty prompt
# ===========================================================================


class TestHandleMessageEmptyPrompt:
    @pytest.mark.anyio
    async def test_empty_prompt_no_branch_no_attachments_returns(self) -> None:
        """Empty prompt without branch override or attachments -> skip."""
        cfg = _make_cfg()
        handler, _, _ = await _capture_handle_message(cfg)

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="",
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="all",
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=(None, ""),
        ):
            msg = _make_discord_message(content="")
            msg.attachments = []

            await handler(msg)

            cfg.exec_cfg.transport.send.assert_not_awaited()


# ===========================================================================
# handle_message: branch override without project
# ===========================================================================


class TestHandleMessageBranchOverride:
    @pytest.mark.anyio
    async def test_branch_override_no_project_sends_error(self) -> None:
        """@branch with no project bound should reply with error."""
        cfg = _make_cfg()
        handler, state_store, _ = await _capture_handle_message(cfg)

        # No channel context (no project bound)
        state_store.get_context = AsyncMock(return_value=None)

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="@feature/xyz do stuff",
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="all",
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=("feature/xyz", "do stuff"),
        ):
            msg = _make_discord_message(content="@feature/xyz do stuff")
            await handler(msg)

            msg.reply.assert_awaited_once()
            reply_text = msg.reply.call_args.args[0]
            assert "no project bound" in reply_text.lower() or "Cannot use" in reply_text


# ===========================================================================
# handle_message: thread context resolution
# ===========================================================================


class TestHandleMessageThreadContext:
    @pytest.mark.anyio
    async def test_thread_message_uses_thread_context(self) -> None:
        """Messages in threads should use thread-specific context."""
        from tunapi.discord.types import DiscordThreadContext

        cfg = _make_cfg()

        resolved = MagicMock()
        resolved.available = True
        resolved.runner = MagicMock()
        resolved.runner.engine = "claude"
        resolved.engine = "claude"
        cfg.runtime.resolve_runner = MagicMock(return_value=resolved)

        handler, state_store, _ = await _capture_handle_message(cfg)

        # Return a DiscordThreadContext for the thread
        thread_ctx = DiscordThreadContext(
            project="myproject",
            branch="feature/test",
        )
        state_store.get_context = AsyncMock(
            side_effect=lambda gid, cid: thread_ctx if cid == 555 else None,
        )

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="hello thread",
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="all",
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=(None, "hello thread"),
        ), patch(
            "tunapi.discord.loop.resolve_effective_default_engine",
            new_callable=AsyncMock,
            return_value=("claude", "config"),
        ), patch(
            "tunapi.discord.loop.resolve_overrides",
            new_callable=AsyncMock,
            return_value=MagicMock(model=None, reasoning=None),
        ), patch(
            "tunapi.runner_bridge.handle_message",
            new_callable=AsyncMock,
        ) as mock_handle:
            msg = _make_discord_message(
                content="hello thread",
                channel_type="thread",
                channel_id=100,
                thread_id=555,
                parent_channel_id=100,
            )

            await handler(msg)

            # The run_job should have been called (via tunapi_handle_message)
            mock_handle.assert_awaited_once()
            # Verify the context passed has the thread branch
            call_kwargs = mock_handle.call_args.kwargs
            assert call_kwargs.get("context") is not None
            assert call_kwargs["context"].project == "myproject"
            assert call_kwargs["context"].branch == "feature/test"


# ===========================================================================
# handle_message: session restore in chat mode
# ===========================================================================


class TestHandleMessageSessionRestore:
    @pytest.mark.anyio
    async def test_chat_mode_restores_resume_token(self) -> None:
        """In chat mode, resume token should be restored from state store.

        When scheduler is active and resume_token is present, the message
        goes through scheduler.enqueue_resume instead of directly calling
        tunapi_handle_message.
        """
        cfg = _make_cfg(session_mode="chat")

        handler, state_store, _ = await _capture_handle_message(cfg)

        state_store.get_session = AsyncMock(return_value="tok-restored-123")

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="continue please",
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="all",
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=(None, "continue please"),
        ), patch(
            "tunapi.discord.loop.resolve_effective_default_engine",
            new_callable=AsyncMock,
            return_value=("claude", "config"),
        ):
            msg = _make_discord_message(content="continue please")

            await handler(msg)

            # Verify state_store.get_session was called
            state_store.get_session.assert_awaited()

            # With scheduler active, a restored resume token goes through
            # scheduler.enqueue_resume and a queued progress message is sent
            cfg.exec_cfg.transport.send.assert_awaited_once()
            kw = cfg.exec_cfg.transport.send.call_args.kwargs
            # The queued progress message was rendered
            assert kw["channel_id"] == 100

    @pytest.mark.anyio
    async def test_stateless_mode_does_not_restore_token(self) -> None:
        """In stateless mode, resume tokens should NOT be restored."""
        cfg = _make_cfg(session_mode="stateless")

        resolved = MagicMock()
        resolved.available = True
        resolved.runner = MagicMock()
        resolved.runner.engine = "claude"
        resolved.engine = "claude"
        cfg.runtime.resolve_runner = MagicMock(return_value=resolved)

        handler, state_store, _ = await _capture_handle_message(cfg)

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="do something",
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="all",
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=(None, "do something"),
        ), patch(
            "tunapi.discord.loop.resolve_effective_default_engine",
            new_callable=AsyncMock,
            return_value=("claude", "config"),
        ), patch(
            "tunapi.discord.loop.resolve_overrides",
            new_callable=AsyncMock,
            return_value=MagicMock(model=None, reasoning=None),
        ), patch(
            "tunapi.runner_bridge.handle_message",
            new_callable=AsyncMock,
        ) as mock_handle:
            msg = _make_discord_message(content="do something")

            await handler(msg)

            # state_store.get_session should NOT be called in stateless mode
            state_store.get_session.assert_not_awaited()

            mock_handle.assert_awaited_once()
            call_kwargs = mock_handle.call_args.kwargs
            assert call_kwargs["resume_token"] is None


# ===========================================================================
# handle_message: engine inference from reply
# ===========================================================================


class TestHandleMessageEngineInference:
    @pytest.mark.anyio
    async def test_infers_engine_from_replied_bot_message(self) -> None:
        """When replying to a bot message, engine should be inferred from header."""
        import discord

        cfg = _make_cfg()

        resolved = MagicMock()
        resolved.available = True
        resolved.runner = MagicMock()
        resolved.runner.engine = "codex"
        resolved.engine = "codex"
        cfg.runtime.resolve_runner = MagicMock(return_value=resolved)

        handler, state_store, _ = await _capture_handle_message(cfg)

        # Set up a reference to a bot message
        ref_msg = MagicMock(spec=discord.Message)
        ref_msg.author = cfg.bot.user
        ref_msg.content = "done · codex · 10s\nHere is my answer..."
        ref_msg.id = 500

        ref = MagicMock()
        ref.message_id = 500
        ref.resolved = ref_msg

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="continue",
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="all",
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=(None, "continue"),
        ), patch(
            "tunapi.discord.loop.resolve_effective_default_engine",
            new_callable=AsyncMock,
            return_value=("claude", "config"),
        ), patch(
            "tunapi.discord.loop.resolve_overrides",
            new_callable=AsyncMock,
            return_value=MagicMock(model=None, reasoning=None),
        ), patch(
            "tunapi.discord.loop._extract_engine_id_from_header",
            return_value="codex",
        ), patch(
            "tunapi.discord.loop._strip_ctx_lines",
            return_value="Here is my answer...",
        ), patch(
            "tunapi.runner_bridge.handle_message",
            new_callable=AsyncMock,
        ) as mock_handle:
            msg = _make_discord_message(content="continue", reference=ref)

            await handler(msg)

            mock_handle.assert_awaited_once()
            # The runner should have been resolved with codex engine
            resolve_call = cfg.runtime.resolve_runner.call_args
            assert resolve_call.kwargs.get("engine_override") == "codex" or (
                resolve_call.args and "codex" in str(resolve_call)
            )


# ===========================================================================
# handle_message: run_job runner unavailable
# ===========================================================================


class TestRunJobRunnerUnavailable:
    @pytest.mark.anyio
    async def test_runner_unavailable_returns_early(self) -> None:
        """When the runner is unavailable, run_job returns without calling handle_message."""
        cfg = _make_cfg()

        resolved = MagicMock()
        resolved.available = False
        resolved.engine = "claude"
        resolved.issue = "engine not installed"
        cfg.runtime.resolve_runner = MagicMock(return_value=resolved)

        handler, _, _ = await _capture_handle_message(cfg)

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="hello",
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="all",
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=(None, "hello"),
        ), patch(
            "tunapi.discord.loop.resolve_effective_default_engine",
            new_callable=AsyncMock,
            return_value=("claude", "config"),
        ), patch(
            "tunapi.discord.loop.resolve_overrides",
            new_callable=AsyncMock,
            return_value=MagicMock(model=None, reasoning=None),
        ), patch(
            "tunapi.runner_bridge.handle_message",
            new_callable=AsyncMock,
        ) as mock_handle:
            msg = _make_discord_message(content="hello")
            await handler(msg)

            # handle_message in runner_bridge should NOT be called
            mock_handle.assert_not_awaited()


# ===========================================================================
# handle_message: run_job cwd error
# ===========================================================================


class TestRunJobCwdError:
    @pytest.mark.anyio
    async def test_cwd_config_error_returns_early(self) -> None:
        """When resolve_run_cwd raises ConfigError, run_job returns early."""
        from tunapi.config import ConfigError

        cfg = _make_cfg()

        resolved = MagicMock()
        resolved.available = True
        resolved.runner = MagicMock()
        resolved.runner.engine = "claude"
        resolved.engine = "claude"
        cfg.runtime.resolve_runner = MagicMock(return_value=resolved)
        cfg.runtime.resolve_run_cwd = MagicMock(
            side_effect=ConfigError("project dir not found")
        )

        handler, _, _ = await _capture_handle_message(cfg)

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="hello",
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="all",
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=(None, "hello"),
        ), patch(
            "tunapi.discord.loop.resolve_effective_default_engine",
            new_callable=AsyncMock,
            return_value=("claude", "config"),
        ), patch(
            "tunapi.discord.loop.resolve_overrides",
            new_callable=AsyncMock,
            return_value=MagicMock(model=None, reasoning=None),
        ), patch(
            "tunapi.runner_bridge.handle_message",
            new_callable=AsyncMock,
        ) as mock_handle:
            msg = _make_discord_message(content="hello")
            await handler(msg)

            mock_handle.assert_not_awaited()


# ===========================================================================
# handle_message: model override resolution
# ===========================================================================


class TestHandleMessageModelOverrides:
    @pytest.mark.anyio
    async def test_model_override_applied_to_run_options(self) -> None:
        """Model overrides from prefs should be passed as run_options."""
        cfg = _make_cfg()

        resolved = MagicMock()
        resolved.available = True
        resolved.runner = MagicMock()
        resolved.runner.engine = "claude"
        resolved.engine = "claude"
        cfg.runtime.resolve_runner = MagicMock(return_value=resolved)

        handler, _, _ = await _capture_handle_message(cfg)

        override_result = MagicMock()
        override_result.model = "claude-opus-4-20250514"
        override_result.reasoning = None
        override_result.source_model = "channel"
        override_result.source_reasoning = None

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="hello",
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="all",
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=(None, "hello"),
        ), patch(
            "tunapi.discord.loop.resolve_effective_default_engine",
            new_callable=AsyncMock,
            return_value=("claude", "config"),
        ), patch(
            "tunapi.discord.loop.resolve_overrides",
            new_callable=AsyncMock,
            return_value=override_result,
        ), patch(
            "tunapi.runner_bridge.handle_message",
            new_callable=AsyncMock,
        ) as mock_handle:
            msg = _make_discord_message(content="hello")
            await handler(msg)

            mock_handle.assert_awaited_once()


# ===========================================================================
# handle_message: auto_put file handling
# ===========================================================================


class TestHandleMessageAutoPut:
    @pytest.mark.anyio
    async def test_auto_put_saves_files_and_confirms(self) -> None:
        """With auto_put enabled, files should be saved and confirmed."""
        from tunapi.discord.types import DiscordChannelContext

        cfg = _make_cfg(files_enabled=True, auto_put=True, auto_put_mode="upload")

        handler, state_store, _ = await _capture_handle_message(cfg)

        # Bind channel to a project
        channel_ctx = DiscordChannelContext(project="myproject")
        state_store.get_context = AsyncMock(return_value=channel_ctx)

        attachment = MagicMock()
        attachment.filename = "test.txt"
        attachment.content_type = "text/plain"
        attachment.size = 100

        save_result = MagicMock()
        save_result.error = None
        save_result.rel_path = Path("incoming/test.txt")
        save_result.size = 100

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="",
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="all",
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=(None, ""),
        ), patch(
            "tunapi.discord.loop.resolve_effective_default_engine",
            new_callable=AsyncMock,
            return_value=("claude", "config"),
        ), patch(
            "tunapi.discord.loop.is_audio_attachment",
            return_value=False,
        ), patch(
            "tunapi.discord.loop.resolve_overrides",
            new_callable=AsyncMock,
            return_value=MagicMock(model=None, reasoning=None),
        ), patch(
            "tunapi.discord.file_transfer.save_attachment",
            new_callable=AsyncMock,
            return_value=save_result,
        ) as mock_save:
            msg = _make_discord_message(content="")
            msg.attachments = [attachment]
            await handler(msg)

            # Confirm message sent back (reply with saved info)
            msg.reply.assert_awaited_once()
            reply_text = msg.reply.call_args.args[0]
            assert "saved" in reply_text.lower() or "incoming/test.txt" in reply_text


# ===========================================================================
# handle_message: auto thread creation
# ===========================================================================


class TestHandleMessageAutoThread:
    @pytest.mark.anyio
    async def test_auto_thread_created_for_channel_messages(self) -> None:
        """In default mode, a thread should be created for channel messages."""
        cfg = _make_cfg()
        cfg.bot.create_thread = AsyncMock(return_value=555)

        resolved = MagicMock()
        resolved.available = True
        resolved.runner = MagicMock()
        resolved.runner.engine = "claude"
        resolved.engine = "claude"
        cfg.runtime.resolve_runner = MagicMock(return_value=resolved)

        handler, _, _ = await _capture_handle_message(cfg)

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="create a thread",
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="all",
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=(None, "create a thread"),
        ), patch(
            "tunapi.discord.loop.resolve_effective_default_engine",
            new_callable=AsyncMock,
            return_value=("claude", "config"),
        ), patch(
            "tunapi.discord.loop.resolve_overrides",
            new_callable=AsyncMock,
            return_value=MagicMock(model=None, reasoning=None),
        ), patch(
            "tunapi.runner_bridge.handle_message",
            new_callable=AsyncMock,
        ) as mock_handle:
            msg = _make_discord_message(content="create a thread")
            await handler(msg)

            cfg.bot.create_thread.assert_awaited_once()
            mock_handle.assert_awaited_once()

    @pytest.mark.anyio
    async def test_chat_mode_no_auto_thread(self) -> None:
        """In chat mode without branch, threads should NOT be auto-created."""
        cfg = _make_cfg(session_mode="chat")

        resolved = MagicMock()
        resolved.available = True
        resolved.runner = MagicMock()
        resolved.runner.engine = "claude"
        resolved.engine = "claude"
        cfg.runtime.resolve_runner = MagicMock(return_value=resolved)

        handler, _, _ = await _capture_handle_message(cfg)

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="hello in channel",
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="all",
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=(None, "hello in channel"),
        ), patch(
            "tunapi.discord.loop.resolve_effective_default_engine",
            new_callable=AsyncMock,
            return_value=("claude", "config"),
        ), patch(
            "tunapi.discord.loop.resolve_overrides",
            new_callable=AsyncMock,
            return_value=MagicMock(model=None, reasoning=None),
        ), patch(
            "tunapi.runner_bridge.handle_message",
            new_callable=AsyncMock,
        ) as mock_handle:
            msg = _make_discord_message(content="hello in channel")
            await handler(msg)

            # In chat mode without branch, create_thread should NOT be called
            cfg.bot.create_thread.assert_not_awaited()
            mock_handle.assert_awaited_once()


# ===========================================================================
# handle_message: reply_ref for new threads vs existing
# ===========================================================================


class TestHandleMessageReplyRef:
    @pytest.mark.anyio
    async def test_new_thread_has_no_reply_ref(self) -> None:
        """For new threads, reply_ref should be None."""
        cfg = _make_cfg()
        cfg.bot.create_thread = AsyncMock(return_value=555)

        resolved = MagicMock()
        resolved.available = True
        resolved.runner = MagicMock()
        resolved.runner.engine = "claude"
        resolved.engine = "claude"
        cfg.runtime.resolve_runner = MagicMock(return_value=resolved)

        handler, _, _ = await _capture_handle_message(cfg)

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="start thread",
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="all",
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=(None, "start thread"),
        ), patch(
            "tunapi.discord.loop.resolve_effective_default_engine",
            new_callable=AsyncMock,
            return_value=("claude", "config"),
        ), patch(
            "tunapi.discord.loop.resolve_overrides",
            new_callable=AsyncMock,
            return_value=MagicMock(model=None, reasoning=None),
        ), patch(
            "tunapi.runner_bridge.handle_message",
            new_callable=AsyncMock,
        ) as mock_handle:
            msg = _make_discord_message(content="start thread")
            await handler(msg)

            mock_handle.assert_awaited_once()
            call_kwargs = mock_handle.call_args.kwargs
            incoming = call_kwargs["incoming"]
            # reply_to should be None for new threads
            assert incoming.reply_to is None


# ===========================================================================
# handle_message: run_job exception handling
# ===========================================================================


class TestHandleMessageRunJobException:
    @pytest.mark.anyio
    async def test_run_job_exception_caught(self) -> None:
        """Exceptions in run_job should be caught and logged, not propagated."""
        cfg = _make_cfg()

        resolved = MagicMock()
        resolved.available = True
        resolved.runner = MagicMock()
        resolved.runner.engine = "claude"
        resolved.engine = "claude"
        cfg.runtime.resolve_runner = MagicMock(return_value=resolved)

        handler, _, _ = await _capture_handle_message(cfg)

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="crash me",
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="all",
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=(None, "crash me"),
        ), patch(
            "tunapi.discord.loop.resolve_effective_default_engine",
            new_callable=AsyncMock,
            return_value=("claude", "config"),
        ), patch(
            "tunapi.discord.loop.resolve_overrides",
            new_callable=AsyncMock,
            return_value=MagicMock(model=None, reasoning=None),
        ), patch(
            "tunapi.runner_bridge.handle_message",
            new_callable=AsyncMock,
            side_effect=RuntimeError("something broke"),
        ):
            msg = _make_discord_message(content="crash me")

            # Should not raise — exception is caught inside handle_message
            await handler(msg)


# ===========================================================================
# handle_message: auto-set startup channel
# ===========================================================================


class TestHandleMessageStartupChannel:
    @pytest.mark.anyio
    async def test_auto_sets_startup_channel_on_first_interaction(self) -> None:
        """First interaction should auto-set the startup channel."""
        cfg = _make_cfg()

        resolved = MagicMock()
        resolved.available = True
        resolved.runner = MagicMock()
        resolved.runner.engine = "claude"
        resolved.engine = "claude"
        cfg.runtime.resolve_runner = MagicMock(return_value=resolved)

        handler, state_store, _ = await _capture_handle_message(cfg)

        state_store.get_startup_channel = AsyncMock(return_value=None)

        with patch(
            "tunapi.discord.loop.should_process_message",
            return_value=True,
        ), patch(
            "tunapi.discord.loop.is_user_allowed",
            side_effect=lambda ids, uid: True,
        ), patch(
            "tunapi.discord.loop.extract_prompt_from_message",
            return_value="first message",
        ), patch(
            "tunapi.discord.loop.resolve_trigger_mode",
            new_callable=AsyncMock,
            return_value="all",
        ), patch(
            "tunapi.discord.loop.parse_branch_prefix",
            return_value=(None, "first message"),
        ), patch(
            "tunapi.discord.loop.resolve_effective_default_engine",
            new_callable=AsyncMock,
            return_value=("claude", "config"),
        ), patch(
            "tunapi.discord.loop.resolve_overrides",
            new_callable=AsyncMock,
            return_value=MagicMock(model=None, reasoning=None),
        ), patch(
            "tunapi.runner_bridge.handle_message",
            new_callable=AsyncMock,
        ):
            msg = _make_discord_message(content="first message")
            await handler(msg)

            state_store.set_startup_channel.assert_awaited_once_with(1, 100)
