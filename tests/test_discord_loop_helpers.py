"""Tests for helper functions in discord/loop.py (not loop_state.py)."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from tunapi.discord.loop import (
    ResumeResolver,
    _save_session_token,
    _send_plain_reply,
    _send_queued_progress,
    _send_startup,
    _wait_for_resume,
    send_with_resume,
)
from tunapi.discord.loop_state import ResumeDecision
from tunapi.model import ResumeToken
from tunapi.transport import MessageRef, RenderedMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(
    *,
    startup_msg: str = "bot started",
    transport: AsyncMock | None = None,
    presenter: MagicMock | None = None,
    runtime: MagicMock | None = None,
) -> MagicMock:
    """Build a minimal DiscordBridgeConfig-like mock."""
    cfg = MagicMock()
    cfg.startup_msg = startup_msg

    t = transport or AsyncMock()
    t.send = AsyncMock(
        return_value=MessageRef(channel_id=1, message_id=99)
    )
    cfg.exec_cfg.transport = t

    p = presenter or MagicMock()
    p.render_progress = MagicMock(
        return_value=RenderedMessage(text="queued...", extra={"show_cancel": False})
    )
    cfg.exec_cfg.presenter = p

    rt = runtime or MagicMock()
    rt.format_context_line = MagicMock(return_value=None)
    cfg.runtime = rt

    return cfg


def _make_running_task(
    *,
    resume: ResumeToken | None = None,
    context=None,
    resume_ready_set: bool = False,
    done_set: bool = False,
) -> MagicMock:
    """Build a minimal RunningTask-like mock with real anyio Events."""
    task = MagicMock()
    task.resume = resume
    task.context = context
    task.resume_ready = anyio.Event()
    task.done = anyio.Event()
    if resume_ready_set:
        task.resume_ready.set()
    if done_set:
        task.done.set()
    return task


# ===========================================================================
# _send_startup
# ===========================================================================


class TestSendStartup:
    @pytest.mark.anyio
    async def test_sends_startup_message(self) -> None:
        cfg = _make_cfg(startup_msg="hello world")
        await _send_startup(cfg, channel_id=42)

        cfg.exec_cfg.transport.send.assert_awaited_once()
        call_kwargs = cfg.exec_cfg.transport.send.call_args.kwargs
        assert call_kwargs["channel_id"] == 42
        msg: RenderedMessage = call_kwargs["message"]
        assert "hello world" in msg.text

    @pytest.mark.anyio
    async def test_no_error_when_send_returns_none(self) -> None:
        cfg = _make_cfg()
        cfg.exec_cfg.transport.send = AsyncMock(return_value=None)
        # Should not raise
        await _send_startup(cfg, channel_id=1)
        cfg.exec_cfg.transport.send.assert_awaited_once()


# ===========================================================================
# _save_session_token
# ===========================================================================


class TestSaveSessionToken:
    @pytest.mark.anyio
    async def test_saves_token(self) -> None:
        state_store = AsyncMock()
        token = ResumeToken(engine="claude", value="tok-123")

        await _save_session_token(
            state_store=state_store,
            guild_id=10,
            session_key=42,
            author_id=7,
            token=token,
        )

        state_store.set_session.assert_awaited_once_with(
            10, 42, "claude", "tok-123", author_id=7,
        )

    @pytest.mark.anyio
    async def test_skips_when_state_store_is_none(self) -> None:
        token = ResumeToken(engine="claude", value="tok-123")
        # Should not raise
        await _save_session_token(
            state_store=None,
            guild_id=10,
            session_key=42,
            author_id=None,
            token=token,
        )

    @pytest.mark.anyio
    async def test_skips_when_guild_id_is_none(self) -> None:
        state_store = AsyncMock()
        token = ResumeToken(engine="claude", value="tok-123")

        await _save_session_token(
            state_store=state_store,
            guild_id=None,
            session_key=42,
            author_id=None,
            token=token,
        )

        state_store.set_session.assert_not_awaited()

    @pytest.mark.anyio
    async def test_author_id_none_passed_through(self) -> None:
        state_store = AsyncMock()
        token = ResumeToken(engine="codex", value="tok-abc")

        await _save_session_token(
            state_store=state_store,
            guild_id=5,
            session_key=100,
            author_id=None,
            token=token,
        )

        state_store.set_session.assert_awaited_once_with(
            5, 100, "codex", "tok-abc", author_id=None,
        )


# ===========================================================================
# _wait_for_resume
# ===========================================================================


class TestWaitForResume:
    @pytest.mark.anyio
    async def test_returns_resume_immediately_when_already_set(self) -> None:
        token = ResumeToken(engine="claude", value="tok-1")
        task = _make_running_task(resume=token)
        result = await _wait_for_resume(task)
        assert result is token

    @pytest.mark.anyio
    async def test_returns_resume_when_resume_ready_fires(self) -> None:
        token = ResumeToken(engine="claude", value="tok-2")
        task = _make_running_task(resume=None)

        async def set_resume_later():
            await anyio.sleep(0.01)
            task.resume = token
            task.resume_ready.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(set_resume_later)
            result = await _wait_for_resume(task)

        assert result is token

    @pytest.mark.anyio
    async def test_returns_none_when_done_fires_without_resume(self) -> None:
        task = _make_running_task(resume=None)

        async def set_done_later():
            await anyio.sleep(0.01)
            task.done.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(set_done_later)
            result = await _wait_for_resume(task)

        assert result is None


# ===========================================================================
# _send_plain_reply
# ===========================================================================


class TestSendPlainReply:
    @pytest.mark.anyio
    async def test_sends_reply_with_text(self) -> None:
        cfg = _make_cfg()
        await _send_plain_reply(
            cfg,
            channel_id=10,
            user_msg_id=20,
            thread_id=None,
            text="some reply",
        )

        cfg.exec_cfg.transport.send.assert_awaited_once()
        kw = cfg.exec_cfg.transport.send.call_args.kwargs
        assert kw["channel_id"] == 10
        assert "some reply" in kw["message"].text
        assert kw["options"].reply_to.message_id == 20
        assert kw["options"].notify is False
        assert kw["message"].extra.get("show_cancel") is False

    @pytest.mark.anyio
    async def test_passes_thread_id(self) -> None:
        cfg = _make_cfg()
        await _send_plain_reply(
            cfg,
            channel_id=10,
            user_msg_id=20,
            thread_id=55,
            text="threaded reply",
        )

        kw = cfg.exec_cfg.transport.send.call_args.kwargs
        assert kw["options"].thread_id == 55
        assert kw["options"].reply_to.thread_id == 55


# ===========================================================================
# _send_queued_progress
# ===========================================================================


class TestSendQueuedProgress:
    @pytest.mark.anyio
    async def test_renders_and_sends_queued_progress(self) -> None:
        cfg = _make_cfg()
        token = ResumeToken(engine="claude", value="tok-q")

        ref = await _send_queued_progress(
            cfg,
            channel_id=10,
            user_msg_id=20,
            thread_id=None,
            resume_token=token,
            context=None,
        )

        # Presenter was called to render progress
        cfg.exec_cfg.presenter.render_progress.assert_called_once()
        call_kwargs = cfg.exec_cfg.presenter.render_progress.call_args.kwargs
        assert call_kwargs["elapsed_s"] == 0.0
        assert call_kwargs["label"] == "queued"

        # Transport send was called
        cfg.exec_cfg.transport.send.assert_awaited_once()
        kw = cfg.exec_cfg.transport.send.call_args.kwargs
        assert kw["channel_id"] == 10
        assert kw["options"].reply_to.message_id == 20

        # Returns the MessageRef from send
        assert ref is not None
        assert ref.message_id == 99

    @pytest.mark.anyio
    async def test_thread_id_forwarded(self) -> None:
        cfg = _make_cfg()
        token = ResumeToken(engine="claude", value="tok-q2")

        await _send_queued_progress(
            cfg,
            channel_id=10,
            user_msg_id=20,
            thread_id=77,
            resume_token=token,
            context=None,
        )

        kw = cfg.exec_cfg.transport.send.call_args.kwargs
        assert kw["options"].thread_id == 77

    @pytest.mark.anyio
    async def test_format_context_line_called(self) -> None:
        cfg = _make_cfg()
        token = ResumeToken(engine="claude", value="tok-q3")
        ctx = MagicMock()

        await _send_queued_progress(
            cfg,
            channel_id=10,
            user_msg_id=20,
            thread_id=None,
            resume_token=token,
            context=ctx,
        )

        cfg.runtime.format_context_line.assert_called_once_with(ctx)


# ===========================================================================
# send_with_resume
# ===========================================================================


class TestSendWithResume:
    @pytest.mark.anyio
    async def test_enqueues_when_resume_available(self) -> None:
        cfg = _make_cfg()
        token = ResumeToken(engine="claude", value="tok-r")
        task = _make_running_task(resume=token)
        enqueue = AsyncMock()

        await send_with_resume(
            cfg, enqueue, task,
            channel_id=10,
            user_msg_id=20,
            thread_id=None,
            session_key=(10, None),
            text="follow up",
        )

        enqueue.assert_awaited_once()
        args = enqueue.call_args.args
        assert args[0] == 10       # channel_id
        assert args[1] == 20       # user_msg_id
        assert args[2] == "follow up"  # text
        assert args[3] is token    # resume

    @pytest.mark.anyio
    async def test_sends_plain_reply_when_no_resume(self) -> None:
        cfg = _make_cfg()
        task = _make_running_task(resume=None, done_set=True)
        enqueue = AsyncMock()

        await send_with_resume(
            cfg, enqueue, task,
            channel_id=10,
            user_msg_id=20,
            thread_id=None,
            session_key=None,
            text="hello",
        )

        enqueue.assert_not_awaited()
        cfg.exec_cfg.transport.send.assert_awaited_once()
        kw = cfg.exec_cfg.transport.send.call_args.kwargs
        assert "resume token not ready" in kw["message"].text

    @pytest.mark.anyio
    async def test_progress_ref_passed_to_enqueue(self) -> None:
        cfg = _make_cfg()
        expected_ref = MessageRef(channel_id=1, message_id=99)
        cfg.exec_cfg.transport.send = AsyncMock(return_value=expected_ref)
        token = ResumeToken(engine="claude", value="tok-r2")
        task = _make_running_task(resume=token)
        enqueue = AsyncMock()

        await send_with_resume(
            cfg, enqueue, task,
            channel_id=10,
            user_msg_id=20,
            thread_id=None,
            session_key=None,
            text="msg",
        )

        # Last arg to enqueue is the progress_ref
        args = enqueue.call_args.args
        assert args[-1] is expected_ref


# ===========================================================================
# ResumeResolver.resolve
# ===========================================================================


class TestResumeResolver:
    def _make_resolver(
        self,
        *,
        running_tasks: dict | None = None,
        enqueue_resume: AsyncMock | None = None,
    ) -> ResumeResolver:
        cfg = _make_cfg()
        tg = MagicMock()
        tg.start_soon = MagicMock()
        return ResumeResolver(
            cfg=cfg,
            task_group=tg,
            running_tasks=running_tasks or {},
            enqueue_resume=enqueue_resume or AsyncMock(),
        )

    @pytest.mark.anyio
    async def test_returns_token_directly_when_provided(self) -> None:
        token = ResumeToken(engine="claude", value="tok-x")
        resolver = self._make_resolver()

        decision = await resolver.resolve(
            resume_token=token,
            reply_id=None,
            chat_id=1,
            user_msg_id=10,
            thread_id=None,
            session_key=None,
            prompt_text="hello",
        )

        assert decision.resume_token is token
        assert decision.handled_by_running_task is False

    @pytest.mark.anyio
    async def test_returns_no_resume_no_reply(self) -> None:
        resolver = self._make_resolver()

        decision = await resolver.resolve(
            resume_token=None,
            reply_id=None,
            chat_id=1,
            user_msg_id=10,
            thread_id=None,
            session_key=None,
            prompt_text="hello",
        )

        assert decision.resume_token is None
        assert decision.handled_by_running_task is False

    @pytest.mark.anyio
    async def test_reply_to_running_task_delegates(self) -> None:
        running_task = _make_running_task(
            resume=ResumeToken(engine="claude", value="tok-y")
        )
        ref = MessageRef(channel_id=1, message_id=50)
        resolver = self._make_resolver(running_tasks={ref: running_task})

        decision = await resolver.resolve(
            resume_token=None,
            reply_id=50,
            chat_id=1,
            user_msg_id=10,
            thread_id=None,
            session_key=None,
            prompt_text="continue",
        )

        assert decision.resume_token is None
        assert decision.handled_by_running_task is True
        # task_group.start_soon should have been called with send_with_resume
        resolver._task_group.start_soon.assert_called_once()
        args = resolver._task_group.start_soon.call_args.args
        assert args[0] is send_with_resume

    @pytest.mark.anyio
    async def test_reply_to_nonexistent_task_returns_no_resume(self) -> None:
        resolver = self._make_resolver(running_tasks={})

        decision = await resolver.resolve(
            resume_token=None,
            reply_id=999,
            chat_id=1,
            user_msg_id=10,
            thread_id=None,
            session_key=None,
            prompt_text="hello",
        )

        assert decision.resume_token is None
        assert decision.handled_by_running_task is False

    @pytest.mark.anyio
    async def test_explicit_token_takes_precedence_over_reply(self) -> None:
        """When both resume_token and reply_id are provided, token wins."""
        token = ResumeToken(engine="claude", value="tok-z")
        running_task = _make_running_task(
            resume=ResumeToken(engine="codex", value="other")
        )
        ref = MessageRef(channel_id=1, message_id=50)
        resolver = self._make_resolver(running_tasks={ref: running_task})

        decision = await resolver.resolve(
            resume_token=token,
            reply_id=50,
            chat_id=1,
            user_msg_id=10,
            thread_id=None,
            session_key=None,
            prompt_text="continue",
        )

        # Explicit token returned directly, no delegation
        assert decision.resume_token is token
        assert decision.handled_by_running_task is False
        resolver._task_group.start_soon.assert_not_called()
