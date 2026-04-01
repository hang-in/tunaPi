"""Tests for tunapi.discord.commands.executor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from tunapi.commands import RunMode, RunRequest, RunResult
from tunapi.config import ConfigError
from tunapi.context import RunContext
from tunapi.model import EngineId, ResumeToken
from tunapi.transport import MessageRef, RenderedMessage, SendOptions
from tunapi.discord.commands.executor import (
    _CaptureTransport,
    _DiscordCommandExecutor,
    _ResumeLineProxy,
    _run_engine,
)


# ---------------------------------------------------------------------------
# _ResumeLineProxy
# ---------------------------------------------------------------------------


class TestResumeLineProxy:
    def test_engine_delegation(self):
        runner = MagicMock()
        runner.engine = "claude"
        proxy = _ResumeLineProxy(runner=runner)
        assert proxy.engine == "claude"

    def test_is_resume_line_delegates(self):
        runner = MagicMock()
        runner.is_resume_line.return_value = True
        proxy = _ResumeLineProxy(runner=runner)
        assert proxy.is_resume_line("resume abc") is True
        runner.is_resume_line.assert_called_once_with("resume abc")

    def test_format_resume_returns_empty(self):
        runner = MagicMock()
        proxy = _ResumeLineProxy(runner=runner)
        token = ResumeToken(engine="claude", value="tok")
        assert proxy.format_resume(token) == ""

    def test_extract_resume_delegates(self):
        runner = MagicMock()
        expected = ResumeToken(engine="claude", value="tok")
        runner.extract_resume.return_value = expected
        proxy = _ResumeLineProxy(runner=runner)
        assert proxy.extract_resume("some text") is expected

    def test_run_delegates(self):
        runner = MagicMock()
        proxy = _ResumeLineProxy(runner=runner)
        token = ResumeToken(engine="claude", value="tok")
        proxy.run("prompt", token)
        runner.run.assert_called_once_with("prompt", token)


# ---------------------------------------------------------------------------
# _CaptureTransport
# ---------------------------------------------------------------------------


class TestCaptureTransport:
    @pytest.mark.anyio
    async def test_send_returns_message_ref(self):
        cap = _CaptureTransport()
        msg = RenderedMessage(text="hello")
        ref = await cap.send(channel_id=1, message=msg)
        assert ref.channel_id == 1
        assert ref.message_id == 1
        assert cap.last_message is msg

    @pytest.mark.anyio
    async def test_send_increments_id(self):
        cap = _CaptureTransport()
        ref1 = await cap.send(channel_id=1, message=RenderedMessage(text="a"))
        ref2 = await cap.send(channel_id=1, message=RenderedMessage(text="b"))
        assert ref2.message_id == 2
        assert ref1.message_id == 1

    @pytest.mark.anyio
    async def test_send_with_thread_id(self):
        cap = _CaptureTransport()
        opts = SendOptions(thread_id=42)
        ref = await cap.send(channel_id=1, message=RenderedMessage(text="t"), options=opts)
        assert ref.thread_id == 42

    @pytest.mark.anyio
    async def test_edit_stores_message(self):
        cap = _CaptureTransport()
        original_ref = MessageRef(channel_id=1, message_id=10)
        msg = RenderedMessage(text="edited")
        ref = await cap.edit(ref=original_ref, message=msg)
        assert ref is original_ref
        assert cap.last_message is msg

    @pytest.mark.anyio
    async def test_delete_returns_true(self):
        cap = _CaptureTransport()
        assert await cap.delete(ref=MessageRef(channel_id=1, message_id=1)) is True

    @pytest.mark.anyio
    async def test_close(self):
        cap = _CaptureTransport()
        assert await cap.close() is None


# ---------------------------------------------------------------------------
# _run_engine
# ---------------------------------------------------------------------------


class TestRunEngine:
    def _make_exec_cfg(self):
        transport = AsyncMock()
        transport.send = AsyncMock(
            return_value=MessageRef(channel_id=1, message_id=100)
        )
        presenter = MagicMock()
        cfg = MagicMock()
        cfg.transport = transport
        cfg.presenter = presenter
        cfg.final_notify = False
        return cfg

    def _make_runtime(self, *, available: bool = True, issue: str | None = None):
        runner = MagicMock()
        runner.engine = "claude"
        resolved = MagicMock()
        resolved.runner = runner
        resolved.available = available
        resolved.issue = issue
        runtime = MagicMock()
        runtime.resolve_runner.return_value = resolved
        runtime.resolve_run_cwd.return_value = None
        runtime.format_context_line.return_value = None
        runtime.is_resume_line = MagicMock(return_value=False)
        return runtime

    @pytest.mark.anyio
    async def test_engine_unavailable_sends_error(self):
        exec_cfg = self._make_exec_cfg()
        runtime = self._make_runtime(available=False, issue="not installed")

        await _run_engine(
            exec_cfg=exec_cfg,
            runtime=runtime,
            running_tasks=None,
            channel_id=1,
            user_msg_id=10,
            text="hello",
            resume_token=None,
            context=None,
        )
        exec_cfg.transport.send.assert_awaited_once()
        call_kwargs = exec_cfg.transport.send.call_args.kwargs
        assert "not installed" in call_kwargs["message"].text

    @pytest.mark.anyio
    async def test_config_error_sends_error(self):
        exec_cfg = self._make_exec_cfg()
        runtime = self._make_runtime(available=True)
        runtime.resolve_run_cwd.side_effect = ConfigError("bad project path")

        await _run_engine(
            exec_cfg=exec_cfg,
            runtime=runtime,
            running_tasks=None,
            channel_id=1,
            user_msg_id=10,
            text="hello",
            resume_token=None,
            context=None,
        )
        exec_cfg.transport.send.assert_awaited_once()
        call_kwargs = exec_cfg.transport.send.call_args.kwargs
        assert "bad project path" in call_kwargs["message"].text

    @pytest.mark.anyio
    async def test_successful_run_calls_handle_message(self):
        exec_cfg = self._make_exec_cfg()
        runtime = self._make_runtime(available=True)

        with patch(
            "tunapi.discord.commands.executor.handle_message",
            new_callable=AsyncMock,
        ) as mock_handle:
            await _run_engine(
                exec_cfg=exec_cfg,
                runtime=runtime,
                running_tasks={},
                channel_id=1,
                user_msg_id=10,
                text="hello world",
                resume_token=None,
                context=None,
            )
            mock_handle.assert_awaited_once()
            call_kwargs = mock_handle.call_args.kwargs
            assert call_kwargs["incoming"].text == "hello world"

    @pytest.mark.anyio
    async def test_exception_in_handle_message_logged(self):
        exec_cfg = self._make_exec_cfg()
        runtime = self._make_runtime(available=True)

        with patch(
            "tunapi.discord.commands.executor.handle_message",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            # Should not raise — exception is caught and logged
            await _run_engine(
                exec_cfg=exec_cfg,
                runtime=runtime,
                running_tasks={},
                channel_id=1,
                user_msg_id=10,
                text="hello",
                resume_token=None,
                context=None,
            )

    @pytest.mark.anyio
    async def test_resume_line_proxy_used_when_show_resume_false(self):
        exec_cfg = self._make_exec_cfg()
        runtime = self._make_runtime(available=True)

        with patch(
            "tunapi.discord.commands.executor.handle_message",
            new_callable=AsyncMock,
        ) as mock_handle:
            await _run_engine(
                exec_cfg=exec_cfg,
                runtime=runtime,
                running_tasks={},
                channel_id=1,
                user_msg_id=10,
                text="hello",
                resume_token=None,
                context=None,
                show_resume_line=False,
            )
            call_kwargs = mock_handle.call_args.kwargs
            runner = call_kwargs["runner"]
            # The runner should be wrapped in _ResumeLineProxy
            assert isinstance(runner, _ResumeLineProxy)

    @pytest.mark.anyio
    async def test_context_fields_in_bind(self):
        exec_cfg = self._make_exec_cfg()
        runtime = self._make_runtime(available=True)
        ctx = RunContext(project="myproj", branch="feat")

        with (
            patch(
                "tunapi.discord.commands.executor.handle_message",
                new_callable=AsyncMock,
            ),
            patch(
                "tunapi.discord.commands.executor.bind_run_context",
            ) as mock_bind,
        ):
            await _run_engine(
                exec_cfg=exec_cfg,
                runtime=runtime,
                running_tasks={},
                channel_id=1,
                user_msg_id=10,
                text="hello",
                resume_token=None,
                context=ctx,
            )
            bind_kwargs = mock_bind.call_args.kwargs
            assert bind_kwargs["project"] == "myproj"
            assert bind_kwargs["branch"] == "feat"

    @pytest.mark.anyio
    async def test_thread_id_forwarded(self):
        exec_cfg = self._make_exec_cfg()
        runtime = self._make_runtime(available=False, issue="n/a")

        await _run_engine(
            exec_cfg=exec_cfg,
            runtime=runtime,
            running_tasks=None,
            channel_id=1,
            user_msg_id=10,
            text="hello",
            resume_token=None,
            context=None,
            thread_id=999,
        )
        call_kwargs = exec_cfg.transport.send.call_args.kwargs
        assert call_kwargs["options"].thread_id == 999


# ---------------------------------------------------------------------------
# _DiscordCommandExecutor
# ---------------------------------------------------------------------------


def _make_executor(
    *,
    runtime: MagicMock | None = None,
    exec_cfg: MagicMock | None = None,
    default_engine: EngineId | None = None,
    thread_id: int | None = None,
) -> _DiscordCommandExecutor:
    if exec_cfg is None:
        transport = AsyncMock()
        transport.send = AsyncMock(
            return_value=MessageRef(channel_id=1, message_id=100)
        )
        exec_cfg = MagicMock()
        exec_cfg.transport = transport
        exec_cfg.presenter = MagicMock()
        exec_cfg.final_notify = False

    if runtime is None:
        runtime = MagicMock()
        runtime.default_context_for_chat.return_value = None
        runtime.resolve_engine.return_value = "claude"

    return _DiscordCommandExecutor(
        exec_cfg=exec_cfg,
        runtime=runtime,
        running_tasks={},
        on_thread_known=None,
        engine_overrides_resolver=None,
        channel_id=1,
        user_msg_id=10,
        thread_id=thread_id,
        guild_id=None,
        show_resume_line=True,
        default_engine_override=default_engine,
    )


class TestDiscordCommandExecutor:
    @pytest.mark.anyio
    async def test_send_string(self):
        executor = _make_executor()
        ref = await executor.send("hello")
        executor._exec_cfg.transport.send.assert_awaited_once()
        call_kwargs = executor._exec_cfg.transport.send.call_args.kwargs
        assert call_kwargs["message"].text == "hello"

    @pytest.mark.anyio
    async def test_send_rendered_message(self):
        executor = _make_executor()
        msg = RenderedMessage(text="rendered")
        await executor.send(msg)
        call_kwargs = executor._exec_cfg.transport.send.call_args.kwargs
        assert call_kwargs["message"] is msg

    @pytest.mark.anyio
    async def test_apply_default_context(self):
        runtime = MagicMock()
        runtime.default_context_for_chat.return_value = RunContext(
            project="proj1", branch=None
        )
        runtime.resolve_engine.return_value = "claude"
        executor = _make_executor(runtime=runtime)

        request = RunRequest(prompt="hello")
        updated = executor._apply_default_context(request)
        assert updated.context is not None
        assert updated.context.project == "proj1"

    def test_apply_default_context_skips_when_set(self):
        executor = _make_executor()
        ctx = RunContext(project="existing", branch=None)
        request = RunRequest(prompt="hello", context=ctx)
        updated = executor._apply_default_context(request)
        assert updated is request

    def test_apply_default_engine(self):
        executor = _make_executor(default_engine="gemini")
        request = RunRequest(prompt="hello")
        updated = executor._apply_default_engine(request)
        assert updated.engine == "gemini"

    def test_apply_default_engine_skips_when_set(self):
        executor = _make_executor(default_engine="gemini")
        request = RunRequest(prompt="hello", engine="codex")
        updated = executor._apply_default_engine(request)
        assert updated.engine == "codex"

    def test_apply_default_engine_no_override(self):
        executor = _make_executor(default_engine=None)
        request = RunRequest(prompt="hello")
        updated = executor._apply_default_engine(request)
        assert updated.engine is None

    @pytest.mark.anyio
    async def test_run_one_emit_mode(self):
        executor = _make_executor()
        with patch(
            "tunapi.discord.commands.executor._run_engine",
            new_callable=AsyncMock,
        ) as mock_run:
            result = await executor.run_one(RunRequest(prompt="hello"), mode="emit")
            assert result.engine == "claude"
            assert result.message is None
            mock_run.assert_awaited_once()

    @pytest.mark.anyio
    async def test_run_one_capture_mode(self):
        executor = _make_executor()
        with patch(
            "tunapi.discord.commands.executor._run_engine",
            new_callable=AsyncMock,
        ) as mock_run:
            result = await executor.run_one(RunRequest(prompt="hello"), mode="capture")
            assert result.engine == "claude"
            # In capture mode, message comes from CaptureTransport (None if _run_engine is mocked)
            mock_run.assert_awaited_once()
            # Verify capture transport was used
            call_kwargs = mock_run.call_args.kwargs
            assert isinstance(call_kwargs["exec_cfg"].transport, _CaptureTransport)

    @pytest.mark.anyio
    async def test_run_many_sequential(self):
        executor = _make_executor()
        with patch(
            "tunapi.discord.commands.executor._run_engine",
            new_callable=AsyncMock,
        ):
            requests = [RunRequest(prompt="a"), RunRequest(prompt="b")]
            results = await executor.run_many(requests, mode="emit", parallel=False)
            assert len(results) == 2

    @pytest.mark.anyio
    async def test_run_many_parallel(self):
        executor = _make_executor()
        with patch(
            "tunapi.discord.commands.executor._run_engine",
            new_callable=AsyncMock,
        ):
            requests = [RunRequest(prompt="a"), RunRequest(prompt="b")]
            results = await executor.run_many(requests, mode="emit", parallel=True)
            assert len(results) == 2

    @pytest.mark.anyio
    async def test_run_one_with_engine_overrides_resolver(self):
        runtime = MagicMock()
        runtime.default_context_for_chat.return_value = None
        runtime.resolve_engine.return_value = "claude"

        run_opts = MagicMock()
        resolver = AsyncMock(return_value=run_opts)

        transport = AsyncMock()
        transport.send = AsyncMock(
            return_value=MessageRef(channel_id=1, message_id=100)
        )
        exec_cfg = MagicMock()
        exec_cfg.transport = transport
        exec_cfg.presenter = MagicMock()
        exec_cfg.final_notify = False

        executor = _DiscordCommandExecutor(
            exec_cfg=exec_cfg,
            runtime=runtime,
            running_tasks={},
            on_thread_known=None,
            engine_overrides_resolver=resolver,
            channel_id=1,
            user_msg_id=10,
            thread_id=None,
            guild_id=None,
            show_resume_line=True,
            default_engine_override=None,
        )

        with patch(
            "tunapi.discord.commands.executor._run_engine",
            new_callable=AsyncMock,
        ) as mock_run:
            await executor.run_one(RunRequest(prompt="hello"), mode="emit")
            resolver.assert_awaited_once_with("claude")
            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["run_options"] is run_opts
