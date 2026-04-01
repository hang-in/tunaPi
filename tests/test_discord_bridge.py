"""Tests for discord bridge: DiscordPresenter, DiscordTransport, config dataclasses."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tunapi.discord.bridge import (
    CancelView,
    ClearView,
    DiscordBridgeConfig,
    DiscordFilesSettings,
    DiscordPresenter,
    DiscordTransport,
    DiscordVoiceMessageSettings,
    _is_cancelled_label,
)
from tunapi.discord.client import SentMessage
from tunapi.markdown import MarkdownFormatter, MarkdownParts
from tunapi.progress import ProgressState
from tunapi.transport import MessageRef, RenderedMessage, SendOptions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(engine: str = "claude") -> ProgressState:
    return ProgressState(
        engine=engine,
        action_count=0,
        actions=(),
        resume=None,
        resume_line=None,
        context_line=None,
    )


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.edit_message = AsyncMock()
    bot.delete_message = AsyncMock()
    bot.close = AsyncMock()
    return bot


def _sent(msg_id: int = 100, channel_id: int = 1, thread_id: int | None = None) -> SentMessage:
    return SentMessage(message_id=msg_id, channel_id=channel_id, thread_id=thread_id)


# ===========================================================================
# _is_cancelled_label
# ===========================================================================

class TestIsCancelledLabel:
    def test_plain_cancelled(self) -> None:
        assert _is_cancelled_label("cancelled") is True

    def test_uppercase_cancelled(self) -> None:
        assert _is_cancelled_label("Cancelled") is True

    def test_backtick_cancelled(self) -> None:
        assert _is_cancelled_label("`cancelled`") is True

    def test_backtick_mixed_case(self) -> None:
        assert _is_cancelled_label("`Cancelled`") is True

    def test_with_whitespace(self) -> None:
        assert _is_cancelled_label("  cancelled  ") is True

    def test_working_label(self) -> None:
        assert _is_cancelled_label("working") is False

    def test_empty(self) -> None:
        assert _is_cancelled_label("") is False

    def test_single_backtick(self) -> None:
        assert _is_cancelled_label("`") is False


# ===========================================================================
# DiscordPresenter
# ===========================================================================

class TestDiscordPresenter:
    def test_render_progress_basic(self) -> None:
        presenter = DiscordPresenter()
        state = _make_state()
        result = presenter.render_progress(state, elapsed_s=5.0, label="working")

        assert isinstance(result, RenderedMessage)
        assert result.extra["show_cancel"] is True
        assert isinstance(result.text, str)

    def test_render_progress_cancelled_hides_cancel_button(self) -> None:
        presenter = DiscordPresenter()
        state = _make_state()
        result = presenter.render_progress(state, elapsed_s=5.0, label="cancelled")

        assert result.extra["show_cancel"] is False

    def test_render_progress_backtick_cancelled(self) -> None:
        presenter = DiscordPresenter()
        state = _make_state()
        result = presenter.render_progress(state, elapsed_s=5.0, label="`cancelled`")

        assert result.extra["show_cancel"] is False

    def test_render_final_trim_mode(self) -> None:
        presenter = DiscordPresenter(message_overflow="trim")
        state = _make_state()
        result = presenter.render_final(
            state, elapsed_s=10.0, status="done", answer="hello"
        )

        assert isinstance(result, RenderedMessage)
        assert result.extra["show_cancel"] is False
        assert "followups" not in result.extra

    def test_render_final_split_mode_short_answer(self) -> None:
        presenter = DiscordPresenter(message_overflow="split")
        state = _make_state()
        result = presenter.render_final(
            state, elapsed_s=10.0, status="done", answer="short"
        )

        assert result.extra["show_cancel"] is False
        # Short answer should not produce followups
        assert "followups" not in result.extra or result.extra.get("followups") is None

    def test_render_final_split_mode_long_answer_produces_followups(self) -> None:
        presenter = DiscordPresenter(message_overflow="split")
        state = _make_state()
        # Very long answer to force splitting
        long_answer = "x" * 5000
        result = presenter.render_final(
            state, elapsed_s=10.0, status="done", answer=long_answer
        )

        assert result.extra["show_cancel"] is False
        followups = result.extra.get("followups")
        if followups is not None:
            assert isinstance(followups, list)
            assert len(followups) >= 1
            for f in followups:
                assert isinstance(f, RenderedMessage)
                assert f.extra.get("show_cancel") is False

    def test_render_final_default_overflow_is_split(self) -> None:
        presenter = DiscordPresenter()
        assert presenter._message_overflow == "split"

    def test_custom_formatter(self) -> None:
        fmt = MarkdownFormatter(max_actions=3)
        presenter = DiscordPresenter(formatter=fmt)
        assert presenter._formatter is fmt


# ===========================================================================
# DiscordTransport
# ===========================================================================

class TestDiscordTransportSend:
    @pytest.mark.anyio
    async def test_send_simple(self) -> None:
        bot = _make_bot()
        bot.send_message.return_value = _sent(100, 1)
        transport = DiscordTransport(bot)

        msg = RenderedMessage(text="hello", extra={"show_cancel": False})
        ref = await transport.send(channel_id=1, message=msg)

        assert ref is not None
        assert ref.channel_id == 1
        assert ref.message_id == 100
        bot.send_message.assert_awaited_once()

    @pytest.mark.anyio
    async def test_send_returns_none_when_bot_fails(self) -> None:
        bot = _make_bot()
        bot.send_message.return_value = None
        transport = DiscordTransport(bot)

        msg = RenderedMessage(text="hello", extra={"show_cancel": False})
        ref = await transport.send(channel_id=1, message=msg)

        assert ref is None

    @pytest.mark.anyio
    async def test_send_with_reply_to(self) -> None:
        bot = _make_bot()
        bot.send_message.return_value = _sent(101, 1)
        transport = DiscordTransport(bot)

        reply_ref = MessageRef(channel_id=1, message_id=50)
        msg = RenderedMessage(text="reply", extra={"show_cancel": False})
        options = SendOptions(reply_to=reply_ref)
        ref = await transport.send(channel_id=1, message=msg, options=options)

        assert ref is not None
        call_kwargs = bot.send_message.call_args.kwargs
        assert call_kwargs["reply_to_message_id"] == 50

    @pytest.mark.anyio
    async def test_send_with_thread_id(self) -> None:
        bot = _make_bot()
        bot.send_message.return_value = _sent(102, 1, thread_id=999)
        transport = DiscordTransport(bot)

        msg = RenderedMessage(text="threaded", extra={"show_cancel": False})
        options = SendOptions(thread_id=999)
        ref = await transport.send(channel_id=1, message=msg, options=options)

        assert ref is not None
        call_kwargs = bot.send_message.call_args.kwargs
        assert call_kwargs["thread_id"] == 999

    @pytest.mark.anyio
    async def test_send_with_replace_deletes_old(self) -> None:
        bot = _make_bot()
        bot.send_message.return_value = _sent(103, 1)
        bot.delete_message.return_value = True
        transport = DiscordTransport(bot)

        old_ref = MessageRef(channel_id=1, message_id=50)
        msg = RenderedMessage(text="new", extra={"show_cancel": False})
        options = SendOptions(replace=old_ref)
        await transport.send(channel_id=1, message=msg, options=options)

        bot.delete_message.assert_awaited_once()

    @pytest.mark.anyio
    async def test_send_with_cancel_button(self) -> None:
        bot = _make_bot()
        bot.send_message.return_value = _sent(104, 1)
        transport = DiscordTransport(bot)

        msg = RenderedMessage(text="progress", extra={"show_cancel": True})
        await transport.send(channel_id=1, message=msg)

        call_kwargs = bot.send_message.call_args.kwargs
        assert isinstance(call_kwargs["view"], CancelView)

    @pytest.mark.anyio
    async def test_send_without_cancel_button(self) -> None:
        bot = _make_bot()
        bot.send_message.return_value = _sent(105, 1)
        transport = DiscordTransport(bot)

        msg = RenderedMessage(text="final", extra={"show_cancel": False})
        await transport.send(channel_id=1, message=msg)

        call_kwargs = bot.send_message.call_args.kwargs
        assert isinstance(call_kwargs["view"], ClearView)

    @pytest.mark.anyio
    async def test_send_followups(self) -> None:
        bot = _make_bot()
        bot.send_message.return_value = _sent(106, 1)
        transport = DiscordTransport(bot)

        followup = RenderedMessage(text="part 2", extra={"show_cancel": False})
        msg = RenderedMessage(
            text="part 1",
            extra={"show_cancel": False, "followups": [followup]},
        )
        await transport.send(channel_id=1, message=msg)

        # Main message + 1 followup = 2 calls
        assert bot.send_message.await_count == 2

    @pytest.mark.anyio
    async def test_send_notifies_message_listener_on_final(self) -> None:
        bot = _make_bot()
        bot.send_message.return_value = _sent(107, 1)
        transport = DiscordTransport(bot)

        listener = AsyncMock()
        transport.add_message_listener(1, listener)

        msg = RenderedMessage(text="done", extra={"show_cancel": False})
        await transport.send(channel_id=1, message=msg)

        listener.assert_awaited_once_with(1, "done", True)

    @pytest.mark.anyio
    async def test_send_does_not_notify_listener_on_progress(self) -> None:
        bot = _make_bot()
        bot.send_message.return_value = _sent(108, 1)
        transport = DiscordTransport(bot)

        listener = AsyncMock()
        transport.add_message_listener(1, listener)

        msg = RenderedMessage(text="working...", extra={"show_cancel": True})
        await transport.send(channel_id=1, message=msg)

        listener.assert_not_awaited()

    @pytest.mark.anyio
    async def test_send_listener_exception_suppressed(self) -> None:
        bot = _make_bot()
        bot.send_message.return_value = _sent(109, 1)
        transport = DiscordTransport(bot)

        listener = AsyncMock(side_effect=RuntimeError("boom"))
        transport.add_message_listener(1, listener)

        msg = RenderedMessage(text="done", extra={"show_cancel": False})
        # Should not raise
        ref = await transport.send(channel_id=1, message=msg)
        assert ref is not None


class TestDiscordTransportEdit:
    @pytest.mark.anyio
    async def test_edit_basic(self) -> None:
        bot = _make_bot()
        bot.edit_message.return_value = _sent(200, 1)
        transport = DiscordTransport(bot)

        ref = MessageRef(channel_id=1, message_id=200)
        msg = RenderedMessage(text="updated", extra={"show_cancel": False})
        result = await transport.edit(ref=ref, message=msg)

        assert result is not None
        assert result.message_id == 200
        bot.edit_message.assert_awaited_once()

    @pytest.mark.anyio
    async def test_edit_returns_none_when_bot_fails_and_wait(self) -> None:
        bot = _make_bot()
        bot.edit_message.return_value = None
        transport = DiscordTransport(bot)

        ref = MessageRef(channel_id=1, message_id=200)
        msg = RenderedMessage(text="updated", extra={"show_cancel": False})
        result = await transport.edit(ref=ref, message=msg, wait=True)

        assert result is None

    @pytest.mark.anyio
    async def test_edit_returns_original_ref_when_no_wait(self) -> None:
        bot = _make_bot()
        bot.edit_message.return_value = None
        transport = DiscordTransport(bot)

        ref = MessageRef(channel_id=1, message_id=200)
        msg = RenderedMessage(text="updated", extra={"show_cancel": False})
        result = await transport.edit(ref=ref, message=msg, wait=False)

        assert result is ref

    @pytest.mark.anyio
    async def test_edit_uses_thread_id_as_channel(self) -> None:
        bot = _make_bot()
        bot.edit_message.return_value = _sent(200, 1)
        transport = DiscordTransport(bot)

        ref = MessageRef(channel_id=1, message_id=200, thread_id=999)
        msg = RenderedMessage(text="updated", extra={"show_cancel": False})
        await transport.edit(ref=ref, message=msg)

        call_kwargs = bot.edit_message.call_args.kwargs
        assert call_kwargs["channel_id"] == 999

    @pytest.mark.anyio
    async def test_edit_with_followups(self) -> None:
        bot = _make_bot()
        bot.edit_message.return_value = _sent(200, 1)
        bot.send_message.return_value = _sent(201, 1)
        transport = DiscordTransport(bot)

        followup = RenderedMessage(text="part 2", extra={"show_cancel": False})
        msg = RenderedMessage(
            text="part 1",
            extra={"show_cancel": False, "followups": [followup]},
        )
        ref = MessageRef(channel_id=1, message_id=200)
        await transport.edit(ref=ref, message=msg)

        bot.edit_message.assert_awaited_once()
        bot.send_message.assert_awaited_once()

    @pytest.mark.anyio
    async def test_edit_with_cancel_view(self) -> None:
        bot = _make_bot()
        bot.edit_message.return_value = _sent(200, 1)
        transport = DiscordTransport(bot)

        ref = MessageRef(channel_id=1, message_id=200)
        msg = RenderedMessage(text="working...", extra={"show_cancel": True})
        await transport.edit(ref=ref, message=msg)

        call_kwargs = bot.edit_message.call_args.kwargs
        assert isinstance(call_kwargs["view"], CancelView)


class TestDiscordTransportDelete:
    @pytest.mark.anyio
    async def test_delete_basic(self) -> None:
        bot = _make_bot()
        bot.delete_message.return_value = True
        transport = DiscordTransport(bot)

        ref = MessageRef(channel_id=1, message_id=300)
        result = await transport.delete(ref=ref)

        assert result is True
        bot.delete_message.assert_awaited_once_with(channel_id=1, message_id=300)

    @pytest.mark.anyio
    async def test_delete_uses_thread_id(self) -> None:
        bot = _make_bot()
        bot.delete_message.return_value = True
        transport = DiscordTransport(bot)

        ref = MessageRef(channel_id=1, message_id=300, thread_id=888)
        await transport.delete(ref=ref)

        bot.delete_message.assert_awaited_once_with(channel_id=888, message_id=300)

    @pytest.mark.anyio
    async def test_delete_returns_false(self) -> None:
        bot = _make_bot()
        bot.delete_message.return_value = False
        transport = DiscordTransport(bot)

        ref = MessageRef(channel_id=1, message_id=300)
        result = await transport.delete(ref=ref)

        assert result is False


class TestDiscordTransportClose:
    @pytest.mark.anyio
    async def test_close_delegates_to_bot(self) -> None:
        bot = _make_bot()
        transport = DiscordTransport(bot)
        await transport.close()
        bot.close.assert_awaited_once()


class TestDiscordTransportListeners:
    def test_add_and_remove_listener(self) -> None:
        bot = _make_bot()
        transport = DiscordTransport(bot)
        listener = AsyncMock()

        transport.add_message_listener(1, listener)
        assert 1 in transport._message_listeners

        transport.remove_message_listener(1)
        assert 1 not in transport._message_listeners

    def test_remove_nonexistent_listener(self) -> None:
        bot = _make_bot()
        transport = DiscordTransport(bot)
        # Should not raise
        transport.remove_message_listener(999)


class TestDiscordTransportCancelHandlers:
    def test_register_and_unregister(self) -> None:
        bot = _make_bot()
        transport = DiscordTransport(bot)
        handler = AsyncMock()

        transport.register_cancel_handler(100, handler)
        assert 100 in transport._cancel_handlers

        transport.unregister_cancel_handler(100)
        assert 100 not in transport._cancel_handlers

    @pytest.mark.anyio
    async def test_handle_cancel_with_registered_handler(self) -> None:
        bot = _make_bot()
        transport = DiscordTransport(bot)
        handler = AsyncMock()
        transport.register_cancel_handler(100, handler)

        interaction = MagicMock()
        interaction.message = MagicMock()
        interaction.message.id = 100
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()

        await transport.handle_cancel_interaction(interaction)

        handler.assert_awaited_once_with(interaction)

    @pytest.mark.anyio
    async def test_handle_cancel_no_handler_defers(self) -> None:
        bot = _make_bot()
        transport = DiscordTransport(bot)

        interaction = MagicMock()
        interaction.message = MagicMock()
        interaction.message.id = 999
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()

        await transport.handle_cancel_interaction(interaction)

        interaction.response.defer.assert_awaited_once()

    @pytest.mark.anyio
    async def test_handle_cancel_no_message_defers(self) -> None:
        bot = _make_bot()
        transport = DiscordTransport(bot)

        interaction = MagicMock()
        interaction.message = None
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()

        await transport.handle_cancel_interaction(interaction)

        interaction.response.defer.assert_awaited_once()


class TestExtractFollowups:
    def test_no_followups_key(self) -> None:
        msg = RenderedMessage(text="x", extra={})
        assert DiscordTransport._extract_followups(msg) == []

    def test_followups_not_list(self) -> None:
        msg = RenderedMessage(text="x", extra={"followups": "invalid"})
        assert DiscordTransport._extract_followups(msg) == []

    def test_followups_filters_non_rendered(self) -> None:
        good = RenderedMessage(text="ok", extra={})
        msg = RenderedMessage(text="x", extra={"followups": [good, "bad", 42]})
        result = DiscordTransport._extract_followups(msg)
        assert result == [good]

    def test_followups_valid_list(self) -> None:
        f1 = RenderedMessage(text="a", extra={})
        f2 = RenderedMessage(text="b", extra={})
        msg = RenderedMessage(text="x", extra={"followups": [f1, f2]})
        result = DiscordTransport._extract_followups(msg)
        assert result == [f1, f2]


# ===========================================================================
# Dataclass configs
# ===========================================================================

class TestDiscordFilesSettings:
    def test_defaults(self) -> None:
        s = DiscordFilesSettings()
        assert s.enabled is False
        assert s.auto_put is True
        assert s.auto_put_mode == "upload"
        assert s.uploads_dir == "incoming"
        assert s.max_upload_bytes == 20 * 1024 * 1024
        assert ".git/**" in s.deny_globs
        assert s.allowed_user_ids is None

    def test_custom_values(self) -> None:
        s = DiscordFilesSettings(
            enabled=True,
            auto_put_mode="prompt",
            max_upload_bytes=1024,
            allowed_user_ids=frozenset({1, 2}),
        )
        assert s.enabled is True
        assert s.auto_put_mode == "prompt"
        assert s.max_upload_bytes == 1024
        assert s.allowed_user_ids == frozenset({1, 2})

    def test_frozen(self) -> None:
        s = DiscordFilesSettings()
        with pytest.raises(AttributeError):
            s.enabled = True  # type: ignore[misc]


class TestDiscordVoiceMessageSettings:
    def test_defaults(self) -> None:
        s = DiscordVoiceMessageSettings()
        assert s.enabled is False
        assert s.max_bytes == 10 * 1024 * 1024
        assert s.whisper_model == "base"

    def test_custom(self) -> None:
        s = DiscordVoiceMessageSettings(enabled=True, whisper_model="large")
        assert s.enabled is True
        assert s.whisper_model == "large"


class TestDiscordBridgeConfig:
    def test_construction(self) -> None:
        bot = _make_bot()
        runtime = MagicMock()
        exec_cfg = MagicMock()

        cfg = DiscordBridgeConfig(
            bot=bot,
            runtime=runtime,
            guild_id=12345,
            startup_msg="hello",
            exec_cfg=exec_cfg,
        )

        assert cfg.bot is bot
        assert cfg.runtime is runtime
        assert cfg.guild_id == 12345
        assert cfg.startup_msg == "hello"
        assert cfg.exec_cfg is exec_cfg
        # Check defaults
        assert cfg.allowed_user_ids is None
        assert cfg.session_mode == "stateless"
        assert cfg.show_resume_line is True
        assert cfg.message_overflow == "split"
        assert cfg.trigger_mode_default == "all"
        assert isinstance(cfg.files, DiscordFilesSettings)
        assert isinstance(cfg.voice_messages, DiscordVoiceMessageSettings)
        assert cfg.media_group_debounce_s == 0.75

    def test_custom_overrides(self) -> None:
        bot = _make_bot()
        runtime = MagicMock()
        exec_cfg = MagicMock()

        cfg = DiscordBridgeConfig(
            bot=bot,
            runtime=runtime,
            guild_id=None,
            startup_msg="hi",
            exec_cfg=exec_cfg,
            session_mode="chat",
            message_overflow="trim",
            trigger_mode_default="mentions",
            allowed_user_ids=frozenset({42}),
        )

        assert cfg.guild_id is None
        assert cfg.session_mode == "chat"
        assert cfg.message_overflow == "trim"
        assert cfg.trigger_mode_default == "mentions"
        assert cfg.allowed_user_ids == frozenset({42})

    def test_frozen(self) -> None:
        bot = _make_bot()
        runtime = MagicMock()
        exec_cfg = MagicMock()
        cfg = DiscordBridgeConfig(
            bot=bot, runtime=runtime, guild_id=None,
            startup_msg="x", exec_cfg=exec_cfg,
        )
        with pytest.raises(AttributeError):
            cfg.session_mode = "chat"  # type: ignore[misc]
