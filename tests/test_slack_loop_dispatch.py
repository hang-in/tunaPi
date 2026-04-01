"""Tests for _dispatch_message, _run_engine, _dispatch_rt_command,
_auto_bind_channel_project, and _resolve_upload_dir in tunapi.slack.loop."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tunapi.core.roundtable import RoundtableSession, RoundtableStore
from tunapi.slack.loop import (
    _auto_bind_channel_project,
    _dispatch_message,
    _dispatch_rt_command,
    _resolve_upload_dir,
    _run_engine,
    _try_dispatch_command,
    _ResolvedPrompt,
)
from tunapi.slack.parsing import SlackMessageEvent
from tunapi.transport import MessageRef, RenderedMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_msg(
    text: str = "",
    channel_id: str = "C1",
    user_id: str = "U1",
    ts: str = "100.0",
    thread_ts: str | None = None,
    files: list | None = None,
) -> SlackMessageEvent:
    return SlackMessageEvent(
        channel_id=channel_id,
        user_id=user_id,
        text=text,
        ts=ts,
        thread_ts=thread_ts,
        files=files,
    )


def _make_cfg(
    *,
    files_enabled: bool = False,
    voice_enabled: bool = False,
    bot_user_id: str = "BOTU",
    channel_id: str | None = "C1",
    session_mode: str = "stateless",
    projects_root: str | None = None,
) -> MagicMock:
    cfg = MagicMock()
    cfg.files_enabled = files_enabled
    cfg.voice_enabled = voice_enabled
    cfg.bot_user_id = bot_user_id
    cfg.channel_id = channel_id
    cfg.session_mode = session_mode
    cfg.startup_msg = "Bot started"
    cfg.files_deny_globs = ()
    cfg.files_max_upload_bytes = 20 * 1024 * 1024
    cfg.files_max_download_bytes = 50 * 1024 * 1024
    cfg.voice_max_bytes = 10 * 1024 * 1024
    cfg.voice_model = "gpt-4o-mini-transcribe"
    cfg.voice_base_url = None
    cfg.voice_api_key = None
    cfg.projects_root = projects_root

    cfg.runtime = MagicMock()
    cfg.runtime.projects_root = projects_root
    cfg.runtime.default_engine = "claude"

    cfg.exec_cfg = MagicMock()
    cfg.exec_cfg.transport = AsyncMock()
    cfg.exec_cfg.transport.send = AsyncMock(
        return_value=MessageRef(channel_id="C1", message_id="200.0")
    )
    return cfg


def _make_resolved_message(
    prompt: str = "hello",
    engine_override: str | None = None,
    context: MagicMock | None = None,
) -> MagicMock:
    rm = MagicMock()
    rm.prompt = prompt
    rm.resume_token = None
    rm.engine_override = engine_override
    rm.context = context
    return rm


def _make_resolved_runner(*, issue: str | None = None) -> MagicMock:
    rr = MagicMock()
    rr.issue = issue
    rr.runner = MagicMock()
    return rr


# ---------------------------------------------------------------------------
# _resolve_upload_dir
# ---------------------------------------------------------------------------


class TestResolveUploadDir:
    def test_uses_project_from_context(self):
        cfg = _make_cfg()
        ctx = MagicMock()
        ctx.project = "myproj"
        cfg.runtime.default_context_for_chat.return_value = ctx

        with patch("tunapi.core.files.resolve_incoming_dir") as mock_resolve:
            mock_resolve.return_value = Path("/tmp/incoming/myproj")
            result = _resolve_upload_dir(cfg, "C1")

        mock_resolve.assert_called_once_with("myproj")
        assert result == Path("/tmp/incoming/myproj")

    def test_falls_back_to_default(self):
        cfg = _make_cfg()
        cfg.runtime.default_context_for_chat.return_value = None

        with patch("tunapi.core.files.resolve_incoming_dir") as mock_resolve:
            mock_resolve.return_value = Path("/tmp/incoming/default")
            result = _resolve_upload_dir(cfg, "C1")

        mock_resolve.assert_called_once_with("default")


# ---------------------------------------------------------------------------
# _auto_bind_channel_project
# ---------------------------------------------------------------------------


class TestAutoBindChannelProject:
    @pytest.mark.anyio()
    async def test_skips_when_no_projects_root(self):
        cfg = _make_cfg(projects_root=None)
        cfg.runtime.projects_root = None
        await _auto_bind_channel_project("C1", cfg)
        cfg.runtime._projects.project_for_chat.assert_not_called()

    @pytest.mark.anyio()
    async def test_skips_when_already_bound(self):
        cfg = _make_cfg(projects_root="/projects")
        cfg.runtime._projects.project_for_chat.return_value = "existing"
        await _auto_bind_channel_project("C1", cfg)
        cfg.bot.get_channel.assert_not_called()

    @pytest.mark.anyio()
    async def test_skips_when_channel_not_found(self):
        cfg = _make_cfg(projects_root="/projects")
        cfg.runtime._projects.project_for_chat.return_value = None
        cfg.bot.get_channel = AsyncMock(return_value=None)
        await _auto_bind_channel_project("C1", cfg)
        cfg.runtime._projects.register_discovered.assert_not_called()

    @pytest.mark.anyio()
    async def test_skips_when_channel_has_no_name(self):
        cfg = _make_cfg(projects_root="/projects")
        cfg.runtime._projects.project_for_chat.return_value = None
        channel = MagicMock()
        channel.name = ""
        cfg.bot.get_channel = AsyncMock(return_value=channel)
        await _auto_bind_channel_project("C1", cfg)
        cfg.runtime._projects.register_discovered.assert_not_called()

    @pytest.mark.anyio()
    async def test_skips_when_root_not_dir(self, tmp_path):
        nonexistent = tmp_path / "nodir"
        cfg = _make_cfg(projects_root=str(nonexistent))
        cfg.runtime._projects.project_for_chat.return_value = None
        channel = MagicMock()
        channel.name = "myproject"
        cfg.bot.get_channel = AsyncMock(return_value=channel)
        await _auto_bind_channel_project("C1", cfg)
        cfg.runtime._projects.register_discovered.assert_not_called()

    @pytest.mark.anyio()
    async def test_binds_matching_directory(self, tmp_path):
        project_dir = tmp_path / "MyProject"
        project_dir.mkdir()

        cfg = _make_cfg(projects_root=str(tmp_path))
        cfg.runtime._projects.project_for_chat.return_value = None
        channel = MagicMock()
        channel.name = "myproject"  # lowercase matches
        cfg.bot.get_channel = AsyncMock(return_value=channel)

        await _auto_bind_channel_project("C1", cfg)

        cfg.runtime._projects.register_discovered.assert_called_once_with(
            alias="MyProject",
            path=project_dir,
            chat_id="C1",
        )

    @pytest.mark.anyio()
    async def test_no_match_in_directory(self, tmp_path):
        other = tmp_path / "other_project"
        other.mkdir()

        cfg = _make_cfg(projects_root=str(tmp_path))
        cfg.runtime._projects.project_for_chat.return_value = None
        channel = MagicMock()
        channel.name = "unrelated"
        cfg.bot.get_channel = AsyncMock(return_value=channel)

        await _auto_bind_channel_project("C1", cfg)
        cfg.runtime._projects.register_discovered.assert_not_called()


# ---------------------------------------------------------------------------
# _run_engine
# ---------------------------------------------------------------------------


class TestRunEngine:
    @pytest.fixture()
    def sessions(self):
        s = AsyncMock()
        s.get = AsyncMock(return_value=None)
        s.set = AsyncMock()
        return s

    @pytest.fixture()
    def chat_prefs(self):
        cp = AsyncMock()
        cp.get_context = AsyncMock(return_value=None)
        cp.get_default_engine = AsyncMock(return_value=None)
        cp.get_engine_model = AsyncMock(return_value=None)
        return cp

    @pytest.mark.anyio()
    async def test_runner_unavailable_sends_warning(self, sessions, chat_prefs):
        cfg = _make_cfg()
        cfg.runtime.resolve_message.return_value = _make_resolved_message()
        cfg.runtime.resolve_engine.return_value = "claude"
        cfg.runtime.format_context_line.return_value = None
        cfg.runtime.resolve_run_cwd.return_value = None
        cfg.runtime.resolve_runner.return_value = _make_resolved_runner(
            issue="claude not installed"
        )

        sent: list[RenderedMessage] = []
        send = AsyncMock(side_effect=lambda m: sent.append(m))
        resolved = _ResolvedPrompt(text="hello", file_context="")

        await _run_engine(resolved, _make_msg(text="hello"), cfg, {}, sessions, chat_prefs, send)

        assert len(sent) == 1
        assert "claude not installed" in sent[0].text

    @pytest.mark.anyio()
    async def test_cwd_error_sends_warning(self, sessions, chat_prefs):
        cfg = _make_cfg()
        cfg.runtime.resolve_message.return_value = _make_resolved_message()
        cfg.runtime.resolve_engine.return_value = "claude"
        cfg.runtime.format_context_line.return_value = None
        cfg.runtime.resolve_run_cwd.side_effect = ValueError("bad project path")

        sent: list[RenderedMessage] = []
        send = AsyncMock(side_effect=lambda m: sent.append(m))
        resolved = _ResolvedPrompt(text="hello", file_context="")

        await _run_engine(resolved, _make_msg(text="hello"), cfg, {}, sessions, chat_prefs, send)

        assert len(sent) == 1
        assert "bad project path" in sent[0].text

    @pytest.mark.anyio()
    async def test_engine_override_from_chat_prefs(self, sessions, chat_prefs):
        chat_prefs.get_default_engine.return_value = "gemini"
        cfg = _make_cfg()
        resolved_msg = _make_resolved_message(engine_override=None)
        cfg.runtime.resolve_message.return_value = resolved_msg
        cfg.runtime.resolve_engine.return_value = "gemini"
        cfg.runtime.format_context_line.return_value = None
        cfg.runtime.resolve_run_cwd.return_value = None
        cfg.runtime.resolve_runner.return_value = _make_resolved_runner(issue="nope")

        send = AsyncMock()
        resolved = _ResolvedPrompt(text="hello", file_context="")

        await _run_engine(resolved, _make_msg(), cfg, {}, sessions, chat_prefs, send)

        # resolve_runner should be called with engine="gemini"
        cfg.runtime.resolve_runner.assert_called_once()
        call_kwargs = cfg.runtime.resolve_runner.call_args[1]
        assert call_kwargs["engine_override"] == "gemini"

    @pytest.mark.anyio()
    async def test_handle_message_called_successfully(self, sessions, chat_prefs):
        cfg = _make_cfg()
        cfg.runtime.resolve_message.return_value = _make_resolved_message()
        cfg.runtime.resolve_engine.return_value = "claude"
        cfg.runtime.format_context_line.return_value = None
        cfg.runtime.resolve_run_cwd.return_value = Path("/tmp/project")
        cfg.runtime.resolve_runner.return_value = _make_resolved_runner()
        cfg.runtime.is_resume_line = MagicMock()

        send = AsyncMock()
        resolved = _ResolvedPrompt(text="hello", file_context="")

        with patch("tunapi.slack.loop.handle_message", new_callable=AsyncMock) as mock_hm, \
             patch("tunapi.slack.loop.set_run_base_dir", return_value="token"), \
             patch("tunapi.slack.loop.reset_run_base_dir"):
            await _run_engine(resolved, _make_msg(), cfg, {}, sessions, chat_prefs, send)

        mock_hm.assert_called_once()

    @pytest.mark.anyio()
    async def test_handle_message_error_logged_not_raised(self, sessions, chat_prefs):
        cfg = _make_cfg()
        cfg.runtime.resolve_message.return_value = _make_resolved_message()
        cfg.runtime.resolve_engine.return_value = "claude"
        cfg.runtime.format_context_line.return_value = None
        cfg.runtime.resolve_run_cwd.return_value = None
        cfg.runtime.resolve_runner.return_value = _make_resolved_runner()
        cfg.runtime.is_resume_line = MagicMock()

        send = AsyncMock()
        resolved = _ResolvedPrompt(text="hello", file_context="")

        with patch("tunapi.slack.loop.handle_message", new_callable=AsyncMock) as mock_hm, \
             patch("tunapi.slack.loop.set_run_base_dir", return_value="token"), \
             patch("tunapi.slack.loop.reset_run_base_dir"):
            mock_hm.side_effect = RuntimeError("boom")
            # Should NOT raise
            await _run_engine(resolved, _make_msg(), cfg, {}, sessions, chat_prefs, send)

    @pytest.mark.anyio()
    async def test_session_mode_chat_gets_resume_token(self, sessions, chat_prefs):
        cfg = _make_cfg(session_mode="chat")
        resume = MagicMock()
        sessions.get.return_value = resume

        cfg.runtime.resolve_message.return_value = _make_resolved_message()
        cfg.runtime.resolve_engine.return_value = "claude"
        cfg.runtime.format_context_line.return_value = None
        cfg.runtime.resolve_run_cwd.return_value = None
        cfg.runtime.resolve_runner.return_value = _make_resolved_runner(issue="stop")

        send = AsyncMock()
        resolved = _ResolvedPrompt(text="hello", file_context="")

        await _run_engine(resolved, _make_msg(), cfg, {}, sessions, chat_prefs, send)

        sessions.get.assert_called_once()

    @pytest.mark.anyio()
    async def test_persona_prefix_resolved(self, sessions, chat_prefs):
        cfg = _make_cfg()
        cfg.runtime.resolve_message.return_value = _make_resolved_message(prompt="@critic review code")
        cfg.runtime.resolve_engine.return_value = "claude"
        cfg.runtime.format_context_line.return_value = None
        cfg.runtime.resolve_run_cwd.return_value = None
        cfg.runtime.resolve_runner.return_value = _make_resolved_runner()
        cfg.runtime.is_resume_line = MagicMock()

        send = AsyncMock()
        resolved = _ResolvedPrompt(text="@critic review code", file_context="")

        with patch("tunapi.slack.loop._resolve_persona_prefix", new_callable=AsyncMock) as mock_rp, \
             patch("tunapi.slack.loop.handle_message", new_callable=AsyncMock), \
             patch("tunapi.slack.loop.set_run_base_dir", return_value="token"), \
             patch("tunapi.slack.loop.reset_run_base_dir"):
            mock_rp.return_value = "[역할: critic]\nBe harsh.\n\n---\n\nreview code"
            await _run_engine(resolved, _make_msg(), cfg, {}, sessions, chat_prefs, send)

        mock_rp.assert_called_once()

    @pytest.mark.anyio()
    async def test_model_override_from_prefs(self, sessions, chat_prefs):
        chat_prefs.get_engine_model.return_value = "claude-sonnet-4-20250514"
        cfg = _make_cfg()
        cfg.runtime.resolve_message.return_value = _make_resolved_message()
        cfg.runtime.resolve_engine.return_value = "claude"
        cfg.runtime.format_context_line.return_value = None
        cfg.runtime.resolve_run_cwd.return_value = None
        cfg.runtime.resolve_runner.return_value = _make_resolved_runner()
        cfg.runtime.is_resume_line = MagicMock()

        send = AsyncMock()
        resolved = _ResolvedPrompt(text="hello", file_context="")

        with patch("tunapi.slack.loop.handle_message", new_callable=AsyncMock), \
             patch("tunapi.slack.loop.set_run_base_dir", return_value="token"), \
             patch("tunapi.slack.loop.reset_run_base_dir"), \
             patch("tunapi.slack.loop.apply_run_options") as mock_apply:
            await _run_engine(resolved, _make_msg(), cfg, {}, sessions, chat_prefs, send)

        chat_prefs.get_engine_model.assert_called_once()


# ---------------------------------------------------------------------------
# _dispatch_message
# ---------------------------------------------------------------------------


class TestDispatchMessage:
    @pytest.fixture()
    def sessions(self):
        return AsyncMock()

    @pytest.fixture()
    def chat_prefs(self):
        cp = AsyncMock()
        cp.get_context = AsyncMock(return_value=None)
        cp.get_default_engine = AsyncMock(return_value=None)
        return cp

    @pytest.mark.anyio()
    async def test_command_short_circuits(self, sessions, chat_prefs):
        cfg = _make_cfg()
        msg = _make_msg(text="!help")

        with patch("tunapi.slack.loop._auto_bind_channel_project", new_callable=AsyncMock), \
             patch("tunapi.slack.loop._try_dispatch_command", new_callable=AsyncMock) as mock_cmd, \
             patch("tunapi.slack.loop._resolve_prompt", new_callable=AsyncMock) as mock_resolve, \
             patch("tunapi.slack.loop._run_engine", new_callable=AsyncMock) as mock_engine:
            mock_cmd.return_value = True
            await _dispatch_message(msg, cfg, {}, sessions, chat_prefs)

        mock_resolve.assert_not_called()
        mock_engine.assert_not_called()

    @pytest.mark.anyio()
    async def test_resolve_none_skips_engine(self, sessions, chat_prefs):
        cfg = _make_cfg()
        msg = _make_msg(text="hello")

        with patch("tunapi.slack.loop._auto_bind_channel_project", new_callable=AsyncMock), \
             patch("tunapi.slack.loop._try_dispatch_command", new_callable=AsyncMock) as mock_cmd, \
             patch("tunapi.slack.loop._resolve_prompt", new_callable=AsyncMock) as mock_resolve, \
             patch("tunapi.slack.loop._run_engine", new_callable=AsyncMock) as mock_engine:
            mock_cmd.return_value = False
            mock_resolve.return_value = None
            await _dispatch_message(msg, cfg, {}, sessions, chat_prefs)

        mock_engine.assert_not_called()

    @pytest.mark.anyio()
    async def test_full_dispatch_calls_run_engine(self, sessions, chat_prefs):
        cfg = _make_cfg()
        msg = _make_msg(text="hello")

        with patch("tunapi.slack.loop._auto_bind_channel_project", new_callable=AsyncMock), \
             patch("tunapi.slack.loop._try_dispatch_command", new_callable=AsyncMock) as mock_cmd, \
             patch("tunapi.slack.loop._resolve_prompt", new_callable=AsyncMock) as mock_resolve, \
             patch("tunapi.slack.loop._run_engine", new_callable=AsyncMock) as mock_engine:
            mock_cmd.return_value = False
            mock_resolve.return_value = _ResolvedPrompt(text="hello", file_context="")
            await _dispatch_message(msg, cfg, {}, sessions, chat_prefs)

        mock_engine.assert_called_once()

    @pytest.mark.anyio()
    async def test_auto_bind_called(self, sessions, chat_prefs):
        cfg = _make_cfg()
        msg = _make_msg(text="!help")

        with patch("tunapi.slack.loop._auto_bind_channel_project", new_callable=AsyncMock) as mock_bind, \
             patch("tunapi.slack.loop._try_dispatch_command", new_callable=AsyncMock, return_value=True):
            await _dispatch_message(msg, cfg, {}, sessions, chat_prefs)

        mock_bind.assert_called_once_with("C1", cfg)

    @pytest.mark.anyio()
    async def test_thread_ts_used_in_send(self, sessions, chat_prefs):
        cfg = _make_cfg()
        msg = _make_msg(text="!help", thread_ts="t1")

        with patch("tunapi.slack.loop._auto_bind_channel_project", new_callable=AsyncMock), \
             patch("tunapi.slack.loop._try_dispatch_command", new_callable=AsyncMock) as mock_cmd:

            async def capture_send(*args, **kwargs):
                # Extract the send callback (7th arg: index 6)
                send_fn = args[6]
                await send_fn(RenderedMessage(text="test"))
                return True

            mock_cmd.side_effect = capture_send
            await _dispatch_message(msg, cfg, {}, sessions, chat_prefs)

        # Transport.send called with thread options
        cfg.exec_cfg.transport.send.assert_called()


# ---------------------------------------------------------------------------
# _dispatch_rt_command
# ---------------------------------------------------------------------------


class TestDispatchRtCommand:
    @pytest.fixture()
    def chat_prefs(self):
        cp = AsyncMock()
        cp.get_context = AsyncMock(return_value=None)
        return cp

    @pytest.mark.anyio()
    async def test_calls_handle_rt(self, chat_prefs):
        cfg = _make_cfg()
        msg = _make_msg(text="!rt test topic")
        send = AsyncMock()

        with patch("tunapi.slack.loop.handle_rt", new_callable=AsyncMock) as mock_rt:
            await _dispatch_rt_command(
                "test topic", msg, cfg, {}, chat_prefs, None, send
            )

        mock_rt.assert_called_once()

    @pytest.mark.anyio()
    async def test_completed_session_provides_continue_and_close(self, chat_prefs):
        cfg = _make_cfg()
        msg = _make_msg(text="!rt follow-up more", thread_ts="t1")
        send = AsyncMock()

        rt_store = RoundtableStore()
        session = RoundtableSession(
            thread_id="t1",
            channel_id="C1",
            topic="original",
            engines=["claude"],
            total_rounds=1,
        )
        rt_store.put(session)
        rt_store.complete("t1")

        with patch("tunapi.slack.loop.handle_rt", new_callable=AsyncMock) as mock_rt:
            await _dispatch_rt_command(
                "follow-up more", msg, cfg, {}, chat_prefs, rt_store, send
            )

        mock_rt.assert_called_once()
        call_kwargs = mock_rt.call_args[1]
        assert call_kwargs["continue_roundtable"] is not None
        assert call_kwargs["close_roundtable"] is not None

    @pytest.mark.anyio()
    async def test_active_session_provides_close_only(self, chat_prefs):
        cfg = _make_cfg()
        msg = _make_msg(text="!rt close", thread_ts="t1")
        send = AsyncMock()

        rt_store = RoundtableStore()
        session = RoundtableSession(
            thread_id="t1",
            channel_id="C1",
            topic="active",
            engines=["claude"],
            total_rounds=1,
        )
        rt_store.put(session)
        # Not completed, so only close is available

        with patch("tunapi.slack.loop.handle_rt", new_callable=AsyncMock) as mock_rt:
            await _dispatch_rt_command(
                "close", msg, cfg, {}, chat_prefs, rt_store, send
            )

        mock_rt.assert_called_once()
        call_kwargs = mock_rt.call_args[1]
        assert call_kwargs["close_roundtable"] is not None
        assert call_kwargs["continue_roundtable"] is None

    @pytest.mark.anyio()
    async def test_no_thread_no_callbacks(self, chat_prefs):
        cfg = _make_cfg()
        msg = _make_msg(text="!rt new topic")  # no thread_ts
        send = AsyncMock()

        with patch("tunapi.slack.loop.handle_rt", new_callable=AsyncMock) as mock_rt:
            await _dispatch_rt_command(
                "new topic", msg, cfg, {}, chat_prefs, RoundtableStore(), send
            )

        call_kwargs = mock_rt.call_args[1]
        assert call_kwargs["continue_roundtable"] is None
        assert call_kwargs["close_roundtable"] is None


# ---------------------------------------------------------------------------
# _try_dispatch_command — additional commands not covered elsewhere
# ---------------------------------------------------------------------------


class TestTryDispatchCommandExtra:
    @pytest.fixture()
    def sessions(self):
        s = AsyncMock()
        s.clear = AsyncMock()
        s.has_any = AsyncMock(return_value=False)
        return s

    @pytest.fixture()
    def chat_prefs(self):
        cp = AsyncMock()
        cp.get_context = AsyncMock(return_value=None)
        cp.get_default_engine = AsyncMock(return_value=None)
        return cp

    @pytest.mark.anyio()
    async def test_persona_dispatches(self, sessions, chat_prefs):
        msg = _make_msg(text="!persona list")
        cfg = _make_cfg()
        send = AsyncMock()

        with patch("tunapi.slack.loop.handle_persona", new_callable=AsyncMock) as mock:
            result = await _try_dispatch_command(msg, cfg, {}, sessions, chat_prefs, None, send)

        assert result is True
        mock.assert_called_once()

    @pytest.mark.anyio()
    async def test_project_dispatches(self, sessions, chat_prefs):
        msg = _make_msg(text="!project list")
        cfg = _make_cfg()
        send = AsyncMock()

        with patch("tunapi.slack.loop.handle_project", new_callable=AsyncMock) as mock:
            result = await _try_dispatch_command(msg, cfg, {}, sessions, chat_prefs, None, send)

        assert result is True
        mock.assert_called_once()

    @pytest.mark.anyio()
    async def test_models_dispatches(self, sessions, chat_prefs):
        msg = _make_msg(text="!models")
        cfg = _make_cfg()
        send = AsyncMock()

        with patch("tunapi.slack.loop.handle_models", new_callable=AsyncMock) as mock:
            result = await _try_dispatch_command(msg, cfg, {}, sessions, chat_prefs, None, send)

        assert result is True
        mock.assert_called_once()

    @pytest.mark.anyio()
    async def test_memory_dispatches(self, sessions, chat_prefs):
        msg = _make_msg(text="!memory list")
        cfg = _make_cfg()
        send = AsyncMock()

        with patch("tunapi.slack.loop.handle_memory", new_callable=AsyncMock) as mock:
            result = await _try_dispatch_command(msg, cfg, {}, sessions, chat_prefs, None, send)

        assert result is True
        mock.assert_called_once()

    @pytest.mark.anyio()
    async def test_branch_dispatches(self, sessions, chat_prefs):
        msg = _make_msg(text="!branch list")
        cfg = _make_cfg()
        send = AsyncMock()

        with patch("tunapi.slack.loop.handle_branch", new_callable=AsyncMock) as mock:
            result = await _try_dispatch_command(msg, cfg, {}, sessions, chat_prefs, None, send)

        assert result is True
        mock.assert_called_once()

    @pytest.mark.anyio()
    async def test_review_dispatches(self, sessions, chat_prefs):
        msg = _make_msg(text="!review list")
        cfg = _make_cfg()
        send = AsyncMock()

        with patch("tunapi.slack.loop.handle_review", new_callable=AsyncMock) as mock:
            result = await _try_dispatch_command(msg, cfg, {}, sessions, chat_prefs, None, send)

        assert result is True
        mock.assert_called_once()

    @pytest.mark.anyio()
    async def test_context_dispatches(self, sessions, chat_prefs):
        msg = _make_msg(text="!context")
        cfg = _make_cfg()
        send = AsyncMock()

        with patch("tunapi.slack.loop.handle_context", new_callable=AsyncMock) as mock:
            result = await _try_dispatch_command(msg, cfg, {}, sessions, chat_prefs, None, send)

        assert result is True
        mock.assert_called_once()

    @pytest.mark.anyio()
    async def test_rt_dispatches(self, sessions, chat_prefs):
        msg = _make_msg(text="!rt start topic")
        cfg = _make_cfg()
        send = AsyncMock()

        with patch("tunapi.slack.loop._dispatch_rt_command", new_callable=AsyncMock) as mock:
            result = await _try_dispatch_command(
                msg, cfg, {}, sessions, chat_prefs, None, send
            )

        assert result is True
        mock.assert_called_once()

    @pytest.mark.anyio()
    async def test_memory_uses_context_project(self, sessions, chat_prefs):
        ctx = MagicMock()
        ctx.project = "myproj"
        chat_prefs.get_context.return_value = ctx
        chat_prefs.get_default_engine.return_value = "gemini"

        msg = _make_msg(text="!memory list")
        cfg = _make_cfg()
        send = AsyncMock()

        with patch("tunapi.slack.loop.handle_memory", new_callable=AsyncMock) as mock:
            await _try_dispatch_command(msg, cfg, {}, sessions, chat_prefs, None, send)

        call_kwargs = mock.call_args[1]
        assert call_kwargs["project"] == "myproj"
        assert call_kwargs["current_engine"] == "gemini"
