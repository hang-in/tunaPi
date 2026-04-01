"""Tests for roundtable (RT) functionality in Telegram and Discord transports,
plus the core handle_rt shared handler."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from tunapi.core.roundtable import (
    RoundtableSession,
    RoundtableStore,
    handle_rt,
    parse_followup_args,
    parse_rt_args,
)
from tunapi.transport import MessageRef, RenderedMessage
from tunapi.transport_runtime import RoundtableConfig


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class FakeRuntime:
    """Minimal stub satisfying the runtime interface for handle_rt."""

    def __init__(
        self,
        engines: list[str] | None = None,
        rt_engines: tuple[str, ...] = (),
        rounds: int = 1,
        max_rounds: int = 3,
    ):
        self._engines = engines if engines is not None else ["claude", "gemini"]
        self.roundtable = RoundtableConfig(
            engines=rt_engines,
            rounds=rounds,
            max_rounds=max_rounds,
        )

    def available_engine_ids(self) -> list[str]:
        return self._engines


def _make_session(
    thread_id: str = "t1",
    channel_id: str | int = "C1",
    topic: str = "test topic",
    engines: list[str] | None = None,
    completed: bool = False,
    transcript: list[tuple[str, str]] | None = None,
) -> RoundtableSession:
    s = RoundtableSession(
        thread_id=thread_id,
        channel_id=channel_id,
        topic=topic,
        engines=engines or ["claude", "gemini"],
        total_rounds=1,
    )
    s.completed = completed
    if transcript:
        s.transcript.extend(transcript)
    return s


# ---------------------------------------------------------------------------
# Core: handle_rt tests
# ---------------------------------------------------------------------------


class TestCoreHandleRt:
    """Tests for the transport-agnostic handle_rt handler."""

    @pytest.mark.anyio()
    async def test_no_engines_sends_error(self):
        runtime = FakeRuntime(engines=[], rt_engines=())
        sent: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            sent.append(msg.text)

        await handle_rt(
            "some topic",
            runtime=runtime,
            send=send,
            start_roundtable=AsyncMock(),
        )

        assert len(sent) == 1
        assert "no engines" in sent[0].lower()

    @pytest.mark.anyio()
    async def test_empty_args_shows_usage(self):
        runtime = FakeRuntime()
        sent: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            sent.append(msg.text)

        await handle_rt(
            "",
            runtime=runtime,
            send=send,
            start_roundtable=AsyncMock(),
        )

        assert len(sent) == 1
        assert "usage" in sent[0].lower()

    @pytest.mark.anyio()
    async def test_start_roundtable_called_with_topic(self):
        runtime = FakeRuntime()
        start = AsyncMock()
        sent: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            sent.append(msg.text)

        await handle_rt(
            "design review",
            runtime=runtime,
            send=send,
            start_roundtable=start,
        )

        start.assert_called_once()
        args = start.call_args[0]
        assert args[0] == "design review"
        assert args[1] == 1  # default rounds
        assert args[2] == ["claude", "gemini"]

    @pytest.mark.anyio()
    async def test_start_roundtable_with_rounds(self):
        runtime = FakeRuntime()
        start = AsyncMock()

        await handle_rt(
            '"topic" --rounds 2',
            runtime=runtime,
            send=AsyncMock(),
            start_roundtable=start,
        )

        start.assert_called_once()
        assert start.call_args[0][1] == 2

    @pytest.mark.anyio()
    async def test_rounds_exceeds_max_sends_error(self):
        runtime = FakeRuntime(max_rounds=3)
        sent: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            sent.append(msg.text)

        await handle_rt(
            '"topic" --rounds 5',
            runtime=runtime,
            send=send,
            start_roundtable=AsyncMock(),
        )

        assert any("maximum" in s.lower() for s in sent)

    @pytest.mark.anyio()
    async def test_close_outside_thread_sends_error(self):
        runtime = FakeRuntime()
        sent: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            sent.append(msg.text)

        await handle_rt(
            "close",
            runtime=runtime,
            send=send,
            start_roundtable=AsyncMock(),
            close_roundtable=None,
            thread_id=None,
        )

        assert any("thread" in s.lower() for s in sent)

    @pytest.mark.anyio()
    async def test_close_calls_close_roundtable(self):
        runtime = FakeRuntime()
        close = AsyncMock()

        await handle_rt(
            "close",
            runtime=runtime,
            send=AsyncMock(),
            start_roundtable=AsyncMock(),
            close_roundtable=close,
            thread_id="t1",
        )

        close.assert_called_once()

    @pytest.mark.anyio()
    async def test_follow_outside_thread_sends_error(self):
        runtime = FakeRuntime()
        sent: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            sent.append(msg.text)

        await handle_rt(
            'follow "new question"',
            runtime=runtime,
            send=send,
            start_roundtable=AsyncMock(),
            continue_roundtable=None,
            thread_id=None,
        )

        assert any("thread" in s.lower() for s in sent)

    @pytest.mark.anyio()
    async def test_follow_calls_continue_roundtable(self):
        runtime = FakeRuntime()
        continue_fn = AsyncMock()

        await handle_rt(
            'follow "new question"',
            runtime=runtime,
            send=AsyncMock(),
            start_roundtable=AsyncMock(),
            continue_roundtable=continue_fn,
            thread_id="t1",
        )

        continue_fn.assert_called_once()
        assert continue_fn.call_args[0][0] == "new question"

    @pytest.mark.anyio()
    async def test_follow_with_engine_filter(self):
        runtime = FakeRuntime()
        continue_fn = AsyncMock()

        await handle_rt(
            'follow claude "question"',
            runtime=runtime,
            send=send_noop,
            start_roundtable=AsyncMock(),
            continue_roundtable=continue_fn,
            thread_id="t1",
        )

        continue_fn.assert_called_once()
        assert continue_fn.call_args[0][1] == ["claude"]

    @pytest.mark.anyio()
    async def test_follow_empty_topic_shows_usage(self):
        runtime = FakeRuntime()
        sent: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            sent.append(msg.text)

        await handle_rt(
            "follow",
            runtime=runtime,
            send=send,
            start_roundtable=AsyncMock(),
            continue_roundtable=AsyncMock(),
            thread_id="t1",
        )

        assert any("usage" in s.lower() for s in sent)

    @pytest.mark.anyio()
    async def test_rt_engines_from_config(self):
        """When rt_config.engines is set, those are used instead of all available."""
        runtime = FakeRuntime(
            engines=["claude", "gemini", "codex"],
            rt_engines=("claude", "gemini"),
        )
        start = AsyncMock()

        await handle_rt(
            "topic",
            runtime=runtime,
            send=send_noop,
            start_roundtable=start,
        )

        start.assert_called_once()
        assert start.call_args[0][2] == ["claude", "gemini"]

    @pytest.mark.anyio()
    async def test_invalid_rounds_value_sends_error(self):
        runtime = FakeRuntime()
        sent: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            sent.append(msg.text)

        await handle_rt(
            '"topic" --rounds abc',
            runtime=runtime,
            send=send,
            start_roundtable=AsyncMock(),
        )

        assert any("invalid" in s.lower() for s in sent)


# Shared no-op send for tests that don't inspect messages.
async def send_noop(msg: RenderedMessage) -> None:
    pass


# ---------------------------------------------------------------------------
# Telegram: _archive_roundtable tests
# ---------------------------------------------------------------------------


class TestTelegramArchiveRoundtable:
    """Tests for the Telegram _archive_roundtable helper."""

    @pytest.mark.anyio()
    async def test_sends_close_message(self):
        from tunapi.telegram.builtin_commands import _archive_roundtable

        session = _make_session()
        sent: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            sent.append(msg.text)

        await _archive_roundtable(session, send)

        assert len(sent) == 1
        assert "closed" in sent[0].lower()

    @pytest.mark.anyio()
    async def test_close_message_with_transcript(self):
        from tunapi.telegram.builtin_commands import _archive_roundtable

        session = _make_session(
            transcript=[("claude", "Answer A"), ("gemini", "Answer B")],
        )
        sent: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            sent.append(msg.text)

        await _archive_roundtable(session, send)

        assert len(sent) == 1
        assert "closed" in sent[0].lower()


# ---------------------------------------------------------------------------
# Telegram: dispatch_rt_command tests
# ---------------------------------------------------------------------------


class TestTelegramDispatchRtCommand:
    """Tests for dispatch_rt_command in the Telegram transport."""

    def _make_ctx_and_msg(
        self,
        *,
        engines: list[str] | None = None,
        rt_engines: tuple[str, ...] = (),
        chat_id: int = 123,
        thread_id: int | None = None,
        roundtable_store: RoundtableStore | None = None,
    ):
        """Build fake TelegramLoopContext and TelegramIncomingMessage."""
        from tunapi.telegram.types import TelegramIncomingMessage

        runtime = FakeRuntime(
            engines=engines if engines is not None else ["claude", "gemini"],
            rt_engines=rt_engines,
        )

        transport = AsyncMock()
        transport.send = AsyncMock(
            return_value=MessageRef(channel_id=chat_id, message_id=999)
        )

        exec_cfg = MagicMock()
        exec_cfg.transport = transport

        cfg = MagicMock()
        cfg.runtime = runtime
        cfg.exec_cfg = exec_cfg

        state = MagicMock()
        state.roundtable_store = (
            roundtable_store if roundtable_store is not None else RoundtableStore()
        )
        state.running_tasks = {}
        state.chat_prefs = None

        ctx = MagicMock()
        ctx.cfg = cfg
        ctx.state = state

        msg = TelegramIncomingMessage(
            transport="telegram",
            chat_id=chat_id,
            message_id=1,
            text="",
            reply_to_message_id=None,
            reply_to_text=None,
            sender_id=42,
            thread_id=thread_id,
        )

        return ctx, msg

    @pytest.mark.anyio()
    async def test_no_roundtable_store_returns_early(self):
        from tunapi.telegram.builtin_commands import dispatch_rt_command

        ctx, msg = self._make_ctx_and_msg()
        ctx.state.roundtable_store = None

        # Should not raise
        await dispatch_rt_command(ctx, msg, "some topic")

    @pytest.mark.anyio()
    async def test_empty_args_shows_usage(self):
        from tunapi.telegram.builtin_commands import dispatch_rt_command

        ctx, msg = self._make_ctx_and_msg()
        sent_msgs: list[RenderedMessage] = []
        ctx.cfg.exec_cfg.transport.send = AsyncMock(
            side_effect=lambda **kw: sent_msgs.append(kw.get("message"))
        )

        await dispatch_rt_command(ctx, msg, "")

        assert len(sent_msgs) == 1
        assert "usage" in sent_msgs[0].text.lower()

    @pytest.mark.anyio()
    async def test_no_engines_sends_error(self):
        from tunapi.telegram.builtin_commands import dispatch_rt_command

        ctx, msg = self._make_ctx_and_msg(engines=[])
        sent_msgs: list[RenderedMessage] = []
        ctx.cfg.exec_cfg.transport.send = AsyncMock(
            side_effect=lambda **kw: sent_msgs.append(kw.get("message"))
        )

        await dispatch_rt_command(ctx, msg, "some topic")

        assert len(sent_msgs) == 1
        assert "no engines" in sent_msgs[0].text.lower()

    @pytest.mark.anyio()
    async def test_start_sends_header(self):
        """dispatch_rt_command with a topic delegates to _start_roundtable
        which sends a header message."""
        from tunapi.telegram.builtin_commands import dispatch_rt_command

        ctx, msg = self._make_ctx_and_msg()

        # Mock run_roundtable to avoid actual execution
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "tunapi.telegram.builtin_commands.run_roundtable",
                AsyncMock(),
            )
            await dispatch_rt_command(ctx, msg, '"design review"')

        # transport.send should have been called (at least the header)
        assert ctx.cfg.exec_cfg.transport.send.call_count >= 1

    @pytest.mark.anyio()
    async def test_close_in_completed_thread(self):
        from tunapi.telegram.builtin_commands import dispatch_rt_command

        store = RoundtableStore()
        session = _make_session(thread_id="100", channel_id=123, completed=True)
        store.put(session)

        ctx, msg = self._make_ctx_and_msg(
            chat_id=123,
            thread_id=100,
            roundtable_store=store,
        )
        sent_msgs: list[RenderedMessage] = []
        ctx.cfg.exec_cfg.transport.send = AsyncMock(
            side_effect=lambda **kw: sent_msgs.append(kw.get("message"))
        )

        await dispatch_rt_command(ctx, msg, "close")

        # Should send "Roundtable closed." and remove from store
        assert any("closed" in m.text.lower() for m in sent_msgs if m)
        assert store.get("100") is None

    @pytest.mark.anyio()
    async def test_close_active_session_sets_cancel(self):
        from tunapi.telegram.builtin_commands import dispatch_rt_command

        store = RoundtableStore()
        session = _make_session(thread_id="200", channel_id=123, completed=False)
        store.put(session)

        ctx, msg = self._make_ctx_and_msg(
            chat_id=123,
            thread_id=200,
            roundtable_store=store,
        )
        sent_msgs: list[RenderedMessage] = []
        ctx.cfg.exec_cfg.transport.send = AsyncMock(
            side_effect=lambda **kw: sent_msgs.append(kw.get("message"))
        )

        await dispatch_rt_command(ctx, msg, "close")

        assert session.cancel_event.is_set()
        assert any("closed" in m.text.lower() for m in sent_msgs if m)

    @pytest.mark.anyio()
    async def test_follow_in_completed_thread(self):
        from tunapi.telegram.builtin_commands import dispatch_rt_command

        store = RoundtableStore()
        session = _make_session(
            thread_id="300",
            channel_id=123,
            completed=True,
            transcript=[("claude", "first answer")],
        )
        store.put(session)

        ctx, msg = self._make_ctx_and_msg(
            chat_id=123,
            thread_id=300,
            roundtable_store=store,
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "tunapi.telegram.builtin_commands.run_followup_round",
                AsyncMock(),
            )
            await dispatch_rt_command(ctx, msg, 'follow "new question"')

        # run_followup_round should have been called (via the monkeypatched mock)

    @pytest.mark.anyio()
    async def test_follow_outside_thread_sends_error(self):
        from tunapi.telegram.builtin_commands import dispatch_rt_command

        ctx, msg = self._make_ctx_and_msg(thread_id=None)
        sent_msgs: list[RenderedMessage] = []
        ctx.cfg.exec_cfg.transport.send = AsyncMock(
            side_effect=lambda **kw: sent_msgs.append(kw.get("message"))
        )

        await dispatch_rt_command(ctx, msg, 'follow "question"')

        assert any("thread" in m.text.lower() for m in sent_msgs if m)

    @pytest.mark.anyio()
    async def test_close_outside_thread_sends_error(self):
        from tunapi.telegram.builtin_commands import dispatch_rt_command

        ctx, msg = self._make_ctx_and_msg(thread_id=None)
        sent_msgs: list[RenderedMessage] = []
        ctx.cfg.exec_cfg.transport.send = AsyncMock(
            side_effect=lambda **kw: sent_msgs.append(kw.get("message"))
        )

        await dispatch_rt_command(ctx, msg, "close")

        assert any("thread" in m.text.lower() for m in sent_msgs if m)


# ---------------------------------------------------------------------------
# Telegram: _start_roundtable tests
# ---------------------------------------------------------------------------


class TestTelegramStartRoundtable:
    """Tests for _start_roundtable in the Telegram transport."""

    @pytest.mark.anyio()
    async def test_header_send_failure_returns_early(self):
        from tunapi.telegram.builtin_commands import _start_roundtable

        transport = AsyncMock()
        transport.send = AsyncMock(return_value=None)

        exec_cfg = MagicMock()
        exec_cfg.transport = transport

        cfg = MagicMock()
        cfg.exec_cfg = exec_cfg

        store = RoundtableStore()

        await _start_roundtable(
            chat_id=123,
            topic="test",
            rounds=1,
            engines=["claude"],
            cfg=cfg,
            running_tasks={},
            chat_prefs=None,
            roundtables=store,
        )

        # No session should be stored
        assert store.get("999") is None

    @pytest.mark.anyio()
    async def test_successful_start_creates_session(self):
        from tunapi.telegram.builtin_commands import _start_roundtable

        transport = AsyncMock()
        transport.send = AsyncMock(
            return_value=MessageRef(channel_id=123, message_id=999)
        )

        exec_cfg = MagicMock()
        exec_cfg.transport = transport

        runtime = FakeRuntime()

        cfg = MagicMock()
        cfg.exec_cfg = exec_cfg
        cfg.runtime = runtime

        store = RoundtableStore()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "tunapi.telegram.builtin_commands.run_roundtable",
                AsyncMock(),
            )
            await _start_roundtable(
                chat_id=123,
                topic="test topic",
                rounds=2,
                engines=["claude", "gemini"],
                cfg=cfg,
                running_tasks={},
                chat_prefs=None,
                roundtables=store,
            )

        # Session should be completed after run_roundtable finishes
        session = store.get("999")
        assert session is not None
        assert session.completed is True

    @pytest.mark.anyio()
    async def test_header_contains_topic_and_engines(self):
        from tunapi.telegram.builtin_commands import _start_roundtable

        sent_messages: list[RenderedMessage] = []
        transport = AsyncMock()

        async def capture_send(**kwargs):
            sent_messages.append(kwargs.get("message"))
            return MessageRef(channel_id=123, message_id=999)

        transport.send = capture_send

        exec_cfg = MagicMock()
        exec_cfg.transport = transport

        cfg = MagicMock()
        cfg.exec_cfg = exec_cfg
        cfg.runtime = FakeRuntime()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "tunapi.telegram.builtin_commands.run_roundtable",
                AsyncMock(),
            )
            await _start_roundtable(
                chat_id=123,
                topic="caching strategy",
                rounds=1,
                engines=["claude", "gemini"],
                cfg=cfg,
                running_tasks={},
                chat_prefs=None,
                roundtables=RoundtableStore(),
            )

        header = sent_messages[0].text
        assert "caching strategy" in header
        assert "`claude`" in header
        assert "`gemini`" in header


# ---------------------------------------------------------------------------
# Discord: _archive_roundtable tests
# ---------------------------------------------------------------------------


class TestDiscordArchiveRoundtable:
    """Tests for the Discord _archive_roundtable helper."""

    @pytest.mark.anyio()
    async def test_sends_close_message(self):
        from tunapi.discord.loop import _archive_roundtable

        session = _make_session(thread_id="t1", channel_id=100)
        transport = AsyncMock()
        exec_cfg = MagicMock()
        exec_cfg.transport = transport

        cfg = MagicMock()
        cfg.exec_cfg = exec_cfg

        await _archive_roundtable(session, cfg)

        transport.send.assert_called_once()
        call_kwargs = transport.send.call_args[1]
        assert "closed" in call_kwargs["message"].text.lower()
        assert call_kwargs["options"].thread_id == "t1"

    @pytest.mark.anyio()
    async def test_sends_to_correct_channel(self):
        from tunapi.discord.loop import _archive_roundtable

        session = _make_session(thread_id="t2", channel_id=456)
        transport = AsyncMock()
        exec_cfg = MagicMock()
        exec_cfg.transport = transport

        cfg = MagicMock()
        cfg.exec_cfg = exec_cfg

        await _archive_roundtable(session, cfg)

        call_kwargs = transport.send.call_args[1]
        assert call_kwargs["channel_id"] == 456


# ---------------------------------------------------------------------------
# Discord: _dispatch_rt_command tests
# ---------------------------------------------------------------------------


class TestDiscordDispatchRtCommand:
    """Tests for _dispatch_rt_command in the Discord transport."""

    def _make_cfg(
        self,
        engines: list[str] | None = None,
        rt_engines: tuple[str, ...] = (),
    ):
        runtime = FakeRuntime(
            engines=engines if engines is not None else ["claude", "gemini"],
            rt_engines=rt_engines,
        )
        transport = AsyncMock()
        transport.send = AsyncMock(
            return_value=MessageRef(channel_id=1, message_id=999)
        )
        exec_cfg = MagicMock()
        exec_cfg.transport = transport

        cfg = MagicMock()
        cfg.runtime = runtime
        cfg.exec_cfg = exec_cfg
        return cfg

    @pytest.mark.anyio()
    async def test_empty_args_shows_usage(self):
        from tunapi.discord.loop import _dispatch_rt_command
        from tunapi.transport import SendOptions

        cfg = self._make_cfg()
        sent: list[RenderedMessage] = []
        cfg.exec_cfg.transport.send = AsyncMock(
            side_effect=lambda **kw: sent.append(kw.get("message"))
        )

        await _dispatch_rt_command(
            "",
            channel_id=1,
            thread_id=None,
            cfg=cfg,
            running_tasks={},
            roundtables=RoundtableStore(),
            run_context=None,
            send_opts=SendOptions(),
        )

        assert len(sent) == 1
        assert "usage" in sent[0].text.lower()

    @pytest.mark.anyio()
    async def test_no_engines_sends_error(self):
        from tunapi.discord.loop import _dispatch_rt_command
        from tunapi.transport import SendOptions

        cfg = self._make_cfg(engines=[])
        sent: list[RenderedMessage] = []
        cfg.exec_cfg.transport.send = AsyncMock(
            side_effect=lambda **kw: sent.append(kw.get("message"))
        )

        await _dispatch_rt_command(
            "some topic",
            channel_id=1,
            thread_id=None,
            cfg=cfg,
            running_tasks={},
            roundtables=RoundtableStore(),
            run_context=None,
            send_opts=SendOptions(),
        )

        assert len(sent) == 1
        assert "no engines" in sent[0].text.lower()

    @pytest.mark.anyio()
    async def test_close_completed_session(self):
        from tunapi.discord.loop import _dispatch_rt_command
        from tunapi.transport import SendOptions

        cfg = self._make_cfg()
        store = RoundtableStore()
        session = _make_session(thread_id="500", channel_id=1, completed=True)
        store.put(session)

        sent: list[RenderedMessage] = []
        cfg.exec_cfg.transport.send = AsyncMock(
            side_effect=lambda **kw: sent.append(kw.get("message"))
        )

        await _dispatch_rt_command(
            "close",
            channel_id=1,
            thread_id=500,
            cfg=cfg,
            running_tasks={},
            roundtables=store,
            run_context=None,
            send_opts=SendOptions(thread_id=500),
        )

        assert any("closed" in m.text.lower() for m in sent if m)
        assert store.get("500") is None

    @pytest.mark.anyio()
    async def test_close_active_session_sets_cancel(self):
        from tunapi.discord.loop import _dispatch_rt_command
        from tunapi.transport import SendOptions

        cfg = self._make_cfg()
        store = RoundtableStore()
        session = _make_session(thread_id="600", channel_id=1, completed=False)
        store.put(session)

        sent: list[RenderedMessage] = []
        cfg.exec_cfg.transport.send = AsyncMock(
            side_effect=lambda **kw: sent.append(kw.get("message"))
        )

        await _dispatch_rt_command(
            "close",
            channel_id=1,
            thread_id=600,
            cfg=cfg,
            running_tasks={},
            roundtables=store,
            run_context=None,
            send_opts=SendOptions(thread_id=600),
        )

        assert session.cancel_event.is_set()

    @pytest.mark.anyio()
    async def test_close_outside_thread_sends_error(self):
        from tunapi.discord.loop import _dispatch_rt_command
        from tunapi.transport import SendOptions

        cfg = self._make_cfg()
        sent: list[RenderedMessage] = []
        cfg.exec_cfg.transport.send = AsyncMock(
            side_effect=lambda **kw: sent.append(kw.get("message"))
        )

        await _dispatch_rt_command(
            "close",
            channel_id=1,
            thread_id=None,
            cfg=cfg,
            running_tasks={},
            roundtables=RoundtableStore(),
            run_context=None,
            send_opts=SendOptions(),
        )

        assert any("thread" in m.text.lower() for m in sent if m)

    @pytest.mark.anyio()
    async def test_follow_in_completed_thread(self):
        from tunapi.discord.loop import _dispatch_rt_command
        from tunapi.transport import SendOptions

        cfg = self._make_cfg()
        store = RoundtableStore()
        session = _make_session(
            thread_id="700",
            channel_id=1,
            completed=True,
            transcript=[("claude", "resp")],
        )
        store.put(session)

        with pytest.MonkeyPatch.context() as mp:
            mock_followup = AsyncMock()
            mp.setattr(
                "tunapi.discord.loop.run_followup_round",
                mock_followup,
            )
            await _dispatch_rt_command(
                'follow "next question"',
                channel_id=1,
                thread_id=700,
                cfg=cfg,
                running_tasks={},
                roundtables=store,
                run_context=None,
                send_opts=SendOptions(thread_id=700),
            )

            mock_followup.assert_called_once()

    @pytest.mark.anyio()
    async def test_start_with_topic(self):
        from tunapi.discord.loop import _dispatch_rt_command
        from tunapi.transport import SendOptions

        cfg = self._make_cfg()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "tunapi.discord.loop.run_roundtable",
                AsyncMock(),
            )
            await _dispatch_rt_command(
                '"test topic"',
                channel_id=1,
                thread_id=None,
                cfg=cfg,
                running_tasks={},
                roundtables=RoundtableStore(),
                run_context=None,
                send_opts=SendOptions(),
            )

        # At minimum the header should have been sent
        assert cfg.exec_cfg.transport.send.call_count >= 1


# ---------------------------------------------------------------------------
# Discord: _start_roundtable tests
# ---------------------------------------------------------------------------


class TestDiscordStartRoundtable:
    """Tests for _start_roundtable in the Discord transport."""

    @pytest.mark.anyio()
    async def test_header_send_failure_returns_early(self):
        from tunapi.discord.loop import _start_roundtable

        transport = AsyncMock()
        transport.send = AsyncMock(return_value=None)

        exec_cfg = MagicMock()
        exec_cfg.transport = transport

        cfg = MagicMock()
        cfg.exec_cfg = exec_cfg

        store = RoundtableStore()

        await _start_roundtable(
            channel_id=1,
            topic="test",
            rounds=1,
            engines=["claude"],
            cfg=cfg,
            running_tasks={},
            roundtables=store,
            run_context=None,
        )

        assert len(store._sessions) == 0

    @pytest.mark.anyio()
    async def test_successful_start_creates_completed_session(self):
        from tunapi.discord.loop import _start_roundtable

        transport = AsyncMock()
        transport.send = AsyncMock(
            return_value=MessageRef(channel_id=1, message_id=888)
        )

        exec_cfg = MagicMock()
        exec_cfg.transport = transport

        runtime = FakeRuntime()
        cfg = MagicMock()
        cfg.exec_cfg = exec_cfg
        cfg.runtime = runtime

        store = RoundtableStore()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "tunapi.discord.loop.run_roundtable",
                AsyncMock(),
            )
            await _start_roundtable(
                channel_id=1,
                topic="architecture",
                rounds=1,
                engines=["claude", "gemini"],
                cfg=cfg,
                running_tasks={},
                roundtables=store,
                run_context=None,
            )

        session = store.get("888")
        assert session is not None
        assert session.completed is True

    @pytest.mark.anyio()
    async def test_session_completed_even_on_error(self):
        """Session should be completed in the finally block even if run_roundtable raises."""
        from tunapi.discord.loop import _start_roundtable

        transport = AsyncMock()
        transport.send = AsyncMock(
            return_value=MessageRef(channel_id=1, message_id=777)
        )

        exec_cfg = MagicMock()
        exec_cfg.transport = transport

        cfg = MagicMock()
        cfg.exec_cfg = exec_cfg

        store = RoundtableStore()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "tunapi.discord.loop.run_roundtable",
                AsyncMock(side_effect=RuntimeError("boom")),
            )
            with pytest.raises(RuntimeError, match="boom"):
                await _start_roundtable(
                    channel_id=1,
                    topic="test",
                    rounds=1,
                    engines=["claude"],
                    cfg=cfg,
                    running_tasks={},
                    roundtables=store,
                    run_context=None,
                )

        session = store.get("777")
        assert session is not None
        assert session.completed is True


# ---------------------------------------------------------------------------
# RoundtableStore tests
# ---------------------------------------------------------------------------


class TestRoundtableStore:
    def test_put_and_get(self):
        store = RoundtableStore()
        session = _make_session(thread_id="s1")
        store.put(session)
        assert store.get("s1") is session

    def test_get_returns_none_for_missing(self):
        store = RoundtableStore()
        assert store.get("nonexistent") is None

    def test_get_completed_returns_only_completed(self):
        store = RoundtableStore()
        session = _make_session(thread_id="s1", completed=False)
        store.put(session)
        assert store.get_completed("s1") is None

        session.completed = True
        assert store.get_completed("s1") is session

    def test_remove(self):
        store = RoundtableStore()
        session = _make_session(thread_id="s1")
        store.put(session)
        removed = store.remove("s1")
        assert removed is session
        assert store.get("s1") is None

    def test_remove_nonexistent(self):
        store = RoundtableStore()
        assert store.remove("no") is None

    def test_complete_marks_session(self):
        store = RoundtableStore()
        session = _make_session(thread_id="s1")
        store.put(session)
        assert not session.completed
        store.complete("s1")
        assert session.completed


# ---------------------------------------------------------------------------
# parse_rt_args / parse_followup_args tests
# ---------------------------------------------------------------------------


class TestParseRtArgs:
    def test_simple_topic(self):
        config = RoundtableConfig(engines=(), rounds=1, max_rounds=3)
        topic, rounds, err = parse_rt_args("design review", config)
        assert topic == "design review"
        assert rounds == 1
        assert err is None

    def test_quoted_topic(self):
        config = RoundtableConfig(engines=(), rounds=1, max_rounds=3)
        topic, rounds, err = parse_rt_args('"design review"', config)
        assert topic == "design review"
        assert err is None

    def test_with_rounds_flag(self):
        config = RoundtableConfig(engines=(), rounds=1, max_rounds=5)
        topic, rounds, err = parse_rt_args('"topic" --rounds 3', config)
        assert topic == "topic"
        assert rounds == 3
        assert err is None

    def test_empty_returns_usage(self):
        config = RoundtableConfig(engines=(), rounds=1, max_rounds=3)
        topic, rounds, err = parse_rt_args("", config)
        assert topic == ""
        assert err is None  # no error, just show usage

    def test_rounds_below_one_errors(self):
        config = RoundtableConfig(engines=(), rounds=1, max_rounds=3)
        _, _, err = parse_rt_args('"t" --rounds 0', config)
        assert err is not None
        assert "at least 1" in err.lower()

    def test_rounds_over_max_errors(self):
        config = RoundtableConfig(engines=(), rounds=1, max_rounds=3)
        _, _, err = parse_rt_args('"t" --rounds 4', config)
        assert err is not None
        assert "maximum" in err.lower()


class TestParseFollowupArgs:
    def test_simple_topic(self):
        topic, engines, err = parse_followup_args("what about caching?", ["claude", "gemini"])
        assert topic == "what about caching?"
        assert engines is None
        assert err is None

    def test_engine_filter(self):
        topic, engines, err = parse_followup_args('claude "question"', ["claude", "gemini"])
        assert topic == "question"
        assert engines == ["claude"]

    def test_multi_engine_filter(self):
        topic, engines, err = parse_followup_args(
            'claude,gemini "question"', ["claude", "gemini"]
        )
        assert topic == "question"
        assert engines == ["claude", "gemini"]

    def test_empty_returns_usage(self):
        topic, engines, err = parse_followup_args("", ["claude"])
        assert topic == ""
        assert err is None
