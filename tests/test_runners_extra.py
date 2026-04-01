"""Tests for GeminiRunner and PiRunner: command building, event translation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tunapi.model import (
    ActionEvent,
    CompletedEvent,
    ResumeToken,
    StartedEvent,
)
from tunapi.schemas import gemini as gemini_schema
from tunapi.schemas import pi as pi_schema
from tunapi.runners.gemini import (
    GeminiRunner,
    GeminiStreamState,
    _tool_kind,
    translate_gemini_event,
    build_runner as gemini_build_runner,
)
from tunapi.runners.pi import (
    PiRunner,
    PiStreamState,
    _looks_like_session_path,
    _short_session_id,
    _maybe_promote_session_id,
    _extract_text_blocks,
    _assistant_error,
    _last_assistant_message,
    translate_pi_event,
    build_runner as pi_build_runner,
    _default_session_dir,
)
from tunapi.config import ConfigError
from tunapi.runners.run_options import EngineRunOptions, set_run_options, reset_run_options


# ---------------------------------------------------------------------------
# Gemini: _tool_kind
# ---------------------------------------------------------------------------


class TestGeminiToolKind:
    def test_file_tools(self) -> None:
        assert _tool_kind("read_file") == "file_change"
        assert _tool_kind("write_file") == "file_change"
        assert _tool_kind("edit_file") == "file_change"
        assert _tool_kind("list_directory") == "file_change"

    def test_command_tools(self) -> None:
        assert _tool_kind("bash") == "command"
        assert _tool_kind("shell_exec") == "command"
        assert _tool_kind("run_command") == "command"

    def test_search_tool(self) -> None:
        assert _tool_kind("search") == "web_search"
        assert _tool_kind("web_search") == "web_search"

    def test_generic_tool(self) -> None:
        assert _tool_kind("something_else") == "tool"


# ---------------------------------------------------------------------------
# Gemini: translate_gemini_event
# ---------------------------------------------------------------------------


class TestTranslateGeminiEvent:
    def test_init_event(self) -> None:
        state = GeminiStreamState()
        event = gemini_schema.GeminiInitEvent(
            session_id="sess-1", model="gemini-2.5-pro"
        )
        results = translate_gemini_event(event, state=state)
        assert len(results) == 1
        started = results[0]
        assert isinstance(started, StartedEvent)
        assert started.resume.engine == "gemini"
        assert started.resume.value == "sess-1"
        assert started.title == "gemini-2.5-pro"
        assert started.meta == {"model": "gemini-2.5-pro"}
        assert state.session_id == "sess-1"

    def test_init_event_no_model(self) -> None:
        state = GeminiStreamState()
        event = gemini_schema.GeminiInitEvent(session_id="s2", model="")
        results = translate_gemini_event(event, state=state)
        started = results[0]
        assert isinstance(started, StartedEvent)
        assert started.title == "gemini"

    def test_message_event_assistant(self) -> None:
        state = GeminiStreamState()
        event = gemini_schema.GeminiMessageEvent(
            role="assistant", content="hello "
        )
        results = translate_gemini_event(event, state=state)
        assert results == []
        assert state.last_assistant_text == "hello "

        event2 = gemini_schema.GeminiMessageEvent(
            role="assistant", content="world"
        )
        translate_gemini_event(event2, state=state)
        assert state.last_assistant_text == "hello world"

    def test_message_event_non_assistant(self) -> None:
        state = GeminiStreamState()
        event = gemini_schema.GeminiMessageEvent(role="user", content="hi")
        results = translate_gemini_event(event, state=state)
        assert results == []

    def test_tool_use_event_file(self) -> None:
        state = GeminiStreamState()
        event = gemini_schema.GeminiToolUseEvent(
            tool_name="write_file",
            tool_id="t1",
            parameters={"file_path": "/tmp/foo.py"},
        )
        results = translate_gemini_event(event, state=state)
        assert len(results) == 1
        action_ev = results[0]
        assert isinstance(action_ev, ActionEvent)
        assert action_ev.phase == "started"
        assert action_ev.action.kind == "file_change"
        assert action_ev.action.title == "/tmp/foo.py"
        assert "t1" in state.pending_actions

    def test_tool_use_event_command(self) -> None:
        state = GeminiStreamState()
        event = gemini_schema.GeminiToolUseEvent(
            tool_name="bash",
            tool_id="t2",
            parameters={"command": "ls"},
        )
        results = translate_gemini_event(event, state=state)
        assert len(results) == 1
        assert results[0].action.kind == "command"

    def test_tool_result_event_success(self) -> None:
        state = GeminiStreamState()
        from tunapi.model import Action

        state.pending_actions["t1"] = Action(
            id="t1", kind="command", title="bash", detail={"name": "bash"}
        )
        event = gemini_schema.GeminiToolResultEvent(
            tool_id="t1", status="success", output="ok output"
        )
        results = translate_gemini_event(event, state=state)
        assert len(results) == 1
        action_ev = results[0]
        assert isinstance(action_ev, ActionEvent)
        assert action_ev.phase == "completed"
        assert action_ev.ok is True
        assert "t1" not in state.pending_actions

    def test_tool_result_event_failure(self) -> None:
        state = GeminiStreamState()
        event = gemini_schema.GeminiToolResultEvent(
            tool_id="unknown", status="error", output="something failed"
        )
        results = translate_gemini_event(event, state=state)
        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].action.kind == "tool"

    def test_result_event_success(self) -> None:
        state = GeminiStreamState()
        state.session_id = "s1"
        state.last_assistant_text = "  final answer  "
        event = gemini_schema.GeminiResultEvent(
            status="success",
            stats=gemini_schema.GeminiResultStats(
                duration_ms=100, total_tokens=500,
                input_tokens=200, output_tokens=300,
            ),
        )
        results = translate_gemini_event(event, state=state)
        assert len(results) == 1
        completed = results[0]
        assert isinstance(completed, CompletedEvent)
        assert completed.ok is True
        assert completed.answer == "final answer"
        assert completed.resume == ResumeToken(engine="gemini", value="s1")
        assert completed.usage == {
            "duration_ms": 100,
            "total_tokens": 500,
            "input_tokens": 200,
            "output_tokens": 300,
        }
        assert completed.error is None

    def test_result_event_failure(self) -> None:
        state = GeminiStreamState()
        state.session_id = "s2"
        event = gemini_schema.GeminiResultEvent(status="error")
        results = translate_gemini_event(event, state=state)
        completed = results[0]
        assert isinstance(completed, CompletedEvent)
        assert completed.ok is False
        assert completed.error == "gemini run failed"

    def test_result_event_no_usage(self) -> None:
        state = GeminiStreamState()
        state.session_id = "s3"
        event = gemini_schema.GeminiResultEvent(
            status="success",
            stats=gemini_schema.GeminiResultStats(),
        )
        results = translate_gemini_event(event, state=state)
        completed = results[0]
        assert completed.usage is None

    def test_unknown_event_returns_empty(self) -> None:
        """Unknown event types should return an empty list."""
        state = GeminiStreamState()

        class FakeEvent:
            pass

        results = translate_gemini_event(FakeEvent(), state=state)  # type: ignore[arg-type]
        assert results == []


# ---------------------------------------------------------------------------
# GeminiRunner: build_args, command, helpers
# ---------------------------------------------------------------------------


class TestGeminiRunner:
    def _runner(self, **kwargs: Any) -> GeminiRunner:
        defaults = dict(
            gemini_cmd="gemini",
            gemini_script=None,
            model="auto",
            yolo=False,
            approval_mode="auto_edit",
            session_title="gemini",
        )
        defaults.update(kwargs)
        return GeminiRunner(**defaults)

    def test_command(self) -> None:
        r = self._runner()
        assert r.command() == "gemini"

    def test_build_args_basic(self) -> None:
        r = self._runner()
        state = r.new_state("hello", None)
        args = r.build_args("hello", None, state=state)
        assert "-p" in args
        assert "hello" in args
        assert "--output-format" in args
        assert "stream-json" in args
        assert "--approval-mode" in args
        assert "auto_edit" in args
        assert "--model" in args
        assert "auto" in args

    def test_build_args_with_resume(self) -> None:
        r = self._runner()
        resume = ResumeToken(engine="gemini", value="s123")
        state = r.new_state("hello", resume)
        args = r.build_args("hello", resume, state=state)
        assert "--resume" in args
        idx = args.index("--resume")
        assert args[idx + 1] == "s123"

    def test_build_args_yolo(self) -> None:
        r = self._runner(yolo=True, approval_mode=None)
        state = r.new_state("hello", None)
        args = r.build_args("hello", None, state=state)
        assert "-y" in args
        assert "--approval-mode" not in args

    def test_build_args_no_approval(self) -> None:
        r = self._runner(approval_mode=None, yolo=False)
        state = r.new_state("hello", None)
        args = r.build_args("hello", None, state=state)
        assert "--approval-mode" not in args
        assert "-y" not in args

    def test_build_args_with_gemini_script(self) -> None:
        r = self._runner(gemini_script="/path/to/index.js")
        state = r.new_state("hello", None)
        args = r.build_args("hello", None, state=state)
        assert "--no-warnings=DEP0040" in args
        assert "/path/to/index.js" in args

    def test_build_args_run_options_override(self) -> None:
        r = self._runner(model="auto")
        state = r.new_state("hello", None)
        tok = set_run_options(EngineRunOptions(model="gemini-2.5-flash"))
        try:
            args = r.build_args("hello", None, state=state)
            idx = args.index("--model")
            assert args[idx + 1] == "gemini-2.5-flash"
        finally:
            reset_run_options(tok)

    def test_build_args_no_model(self) -> None:
        r = self._runner(model=None)
        state = r.new_state("hello", None)
        args = r.build_args("hello", None, state=state)
        assert "--model" not in args

    def test_format_resume(self) -> None:
        r = self._runner()
        token = ResumeToken(engine="gemini", value="abc")
        assert r.format_resume(token) == "`gemini --resume abc`"

    def test_stdin_payload_none(self) -> None:
        r = self._runner()
        assert r.stdin_payload("hello", None, state=GeminiStreamState()) is None

    def test_env_none(self) -> None:
        r = self._runner()
        assert r.env(state=GeminiStreamState()) is None

    def test_process_error_events(self) -> None:
        r = self._runner()
        state = r.new_state("hello", None)
        events = r.process_error_events(
            1, resume=None, found_session=None, state=state
        )
        assert len(events) == 2
        assert isinstance(events[1], CompletedEvent)
        assert events[1].ok is False
        assert "rc=1" in events[1].error

    def test_stream_end_events_no_session(self) -> None:
        r = self._runner()
        state = r.new_state("hello", None)
        events = r.stream_end_events(
            resume=None, found_session=None, state=state
        )
        assert len(events) == 1
        completed = events[0]
        assert isinstance(completed, CompletedEvent)
        assert "no session_id" in completed.error

    def test_stream_end_events_with_session(self) -> None:
        r = self._runner()
        state = r.new_state("hello", None)
        state.last_assistant_text = "partial"
        found = ResumeToken(engine="gemini", value="s1")
        events = r.stream_end_events(
            resume=None, found_session=found, state=state
        )
        completed = events[0]
        assert isinstance(completed, CompletedEvent)
        assert completed.answer == "partial"
        assert completed.resume == found


# ---------------------------------------------------------------------------
# gemini build_runner
# ---------------------------------------------------------------------------


class TestGeminiBuildRunner:
    def test_build_runner_defaults(self) -> None:
        config: dict[str, Any] = {}
        runner = gemini_build_runner(config, Path("/fake"))
        assert isinstance(runner, GeminiRunner)
        assert runner.model == "auto"
        assert runner.yolo is False

    def test_build_runner_custom(self) -> None:
        config: dict[str, Any] = {
            "model": "gemini-2.5-pro",
            "yolo": True,
            "approval_mode": "full",
        }
        runner = gemini_build_runner(config, Path("/fake"))
        assert isinstance(runner, GeminiRunner)
        assert runner.model == "gemini-2.5-pro"
        assert runner.yolo is True
        assert runner.approval_mode == "full"


# ===========================================================================
# Pi Runner Tests
# ===========================================================================


class TestPiHelpers:
    def test_looks_like_session_path(self) -> None:
        assert _looks_like_session_path("foo.jsonl") is True
        assert _looks_like_session_path("/some/path") is True
        assert _looks_like_session_path("~/.pi/sessions") is True
        assert _looks_like_session_path("C:\\foo") is True
        assert _looks_like_session_path("abc-def") is False
        assert _looks_like_session_path("") is False

    def test_short_session_id(self) -> None:
        assert _short_session_id("abc-def-ghi") == "abc"
        assert _short_session_id("abcdefghij") == "abcdefgh"
        assert _short_session_id("short") == "short"
        assert _short_session_id("") == ""

    def test_maybe_promote_session_id(self) -> None:
        token = ResumeToken(engine="pi", value="/path/to/session.jsonl")
        state = PiStreamState(resume=token, allow_id_promotion=True)
        _maybe_promote_session_id(state, "new-session-id-long")
        assert state.resume.value == "new"
        assert state.allow_id_promotion is False

    def test_maybe_promote_no_op_when_started(self) -> None:
        token = ResumeToken(engine="pi", value="/path.jsonl")
        state = PiStreamState(resume=token, allow_id_promotion=True, started=True)
        _maybe_promote_session_id(state, "new-id")
        assert state.resume.value == "/path.jsonl"

    def test_maybe_promote_no_op_when_not_path(self) -> None:
        token = ResumeToken(engine="pi", value="simple-id")
        state = PiStreamState(resume=token, allow_id_promotion=True)
        _maybe_promote_session_id(state, "new-id")
        assert state.resume.value == "simple-id"

    def test_maybe_promote_no_op_when_disabled(self) -> None:
        token = ResumeToken(engine="pi", value="/path.jsonl")
        state = PiStreamState(resume=token, allow_id_promotion=False)
        _maybe_promote_session_id(state, "new-id")
        assert state.resume.value == "/path.jsonl"

    def test_maybe_promote_none_session(self) -> None:
        token = ResumeToken(engine="pi", value="/path.jsonl")
        state = PiStreamState(resume=token, allow_id_promotion=True)
        _maybe_promote_session_id(state, None)
        assert state.resume.value == "/path.jsonl"

    def test_extract_text_blocks(self) -> None:
        content = [
            {"type": "text", "text": "hello "},
            {"type": "image", "data": "..."},
            {"type": "text", "text": "world"},
        ]
        assert _extract_text_blocks(content) == "hello world"

    def test_extract_text_blocks_empty(self) -> None:
        assert _extract_text_blocks([]) is None
        assert _extract_text_blocks(None) is None
        assert _extract_text_blocks("not a list") is None
        assert _extract_text_blocks([{"type": "text", "text": ""}]) is None

    def test_assistant_error(self) -> None:
        assert _assistant_error({"stopReason": "error", "errorMessage": "oops"}) == "oops"
        assert _assistant_error({"stopReason": "error"}) == "pi run error"
        assert _assistant_error({"stopReason": "aborted"}) == "pi run aborted"
        assert _assistant_error({"stopReason": "end_turn"}) is None

    def test_last_assistant_message(self) -> None:
        msgs: list[dict[str, Any]] = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "bye"},
        ]
        result = _last_assistant_message(msgs)
        assert result is not None
        assert result["content"] == "hello"

    def test_last_assistant_message_none(self) -> None:
        assert _last_assistant_message(None) is None
        assert _last_assistant_message([{"role": "user"}]) is None


# ---------------------------------------------------------------------------
# Pi: translate_pi_event
# ---------------------------------------------------------------------------


class TestTranslatePiEvent:
    def _resume(self) -> ResumeToken:
        return ResumeToken(engine="pi", value="sess-1")

    def _meta(self) -> dict[str, Any]:
        return {"model": "test-model"}

    def test_session_header_starts(self) -> None:
        state = PiStreamState(resume=self._resume())
        event = pi_schema.SessionHeader(id="sess-1")
        results = translate_pi_event(
            event, title="pi", meta=self._meta(), state=state
        )
        assert len(results) == 1
        assert isinstance(results[0], StartedEvent)
        assert state.started is True

    def test_session_header_duplicate(self) -> None:
        state = PiStreamState(resume=self._resume(), started=True)
        event = pi_schema.SessionHeader(id="sess-1")
        results = translate_pi_event(
            event, title="pi", meta=self._meta(), state=state
        )
        assert results == []

    def test_tool_execution_start(self) -> None:
        state = PiStreamState(resume=self._resume(), started=True)
        event = pi_schema.ToolExecutionStart(
            toolCallId="tc1", toolName="bash", args={"command": "ls"}
        )
        results = translate_pi_event(
            event, title="pi", meta=self._meta(), state=state
        )
        action_events = [e for e in results if isinstance(e, ActionEvent)]
        assert len(action_events) == 1
        assert action_events[0].action.kind == "command"
        assert "tc1" in state.pending_actions

    def test_tool_execution_start_auto_starts(self) -> None:
        """Tool event before session header should auto-emit StartedEvent."""
        state = PiStreamState(resume=self._resume())
        event = pi_schema.ToolExecutionStart(
            toolCallId="tc1", toolName="bash", args={"command": "ls"}
        )
        results = translate_pi_event(
            event, title="pi", meta=self._meta(), state=state
        )
        assert state.started is True
        assert any(isinstance(e, StartedEvent) for e in results)

    def test_tool_execution_start_empty_id(self) -> None:
        state = PiStreamState(resume=self._resume(), started=True)
        event = pi_schema.ToolExecutionStart(
            toolCallId="", toolName="bash", args={}
        )
        results = translate_pi_event(
            event, title="pi", meta=self._meta(), state=state
        )
        action_events = [e for e in results if isinstance(e, ActionEvent)]
        assert len(action_events) == 0

    def test_tool_execution_end(self) -> None:
        state = PiStreamState(resume=self._resume(), started=True)
        from tunapi.model import Action

        state.pending_actions["tc1"] = Action(
            id="tc1", kind="command", title="bash", detail={}
        )
        event = pi_schema.ToolExecutionEnd(
            toolCallId="tc1", toolName="bash", result="ok", isError=False
        )
        results = translate_pi_event(
            event, title="pi", meta=self._meta(), state=state
        )
        action_events = [e for e in results if isinstance(e, ActionEvent)]
        assert len(action_events) == 1
        assert action_events[0].phase == "completed"
        assert action_events[0].ok is True

    def test_tool_execution_end_unknown_action(self) -> None:
        state = PiStreamState(resume=self._resume(), started=True)
        event = pi_schema.ToolExecutionEnd(
            toolCallId="tc99", toolName="unknown", result="err", isError=True
        )
        results = translate_pi_event(
            event, title="pi", meta=self._meta(), state=state
        )
        action_events = [e for e in results if isinstance(e, ActionEvent)]
        assert len(action_events) == 1
        assert action_events[0].ok is False

    def test_tool_execution_end_empty_id(self) -> None:
        state = PiStreamState(resume=self._resume(), started=True)
        event = pi_schema.ToolExecutionEnd(
            toolCallId="", toolName="bash", result="ok"
        )
        results = translate_pi_event(
            event, title="pi", meta=self._meta(), state=state
        )
        action_events = [e for e in results if isinstance(e, ActionEvent)]
        assert len(action_events) == 0

    def test_message_end_assistant(self) -> None:
        state = PiStreamState(resume=self._resume(), started=True)
        event = pi_schema.MessageEnd(
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": "answer text"}],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            }
        )
        results = translate_pi_event(
            event, title="pi", meta=self._meta(), state=state
        )
        # MessageEnd doesn't produce events directly
        assert all(not isinstance(e, CompletedEvent) for e in results)
        assert state.last_assistant_text == "answer text"
        assert state.last_usage == {"input_tokens": 10, "output_tokens": 20}

    def test_message_end_with_error(self) -> None:
        state = PiStreamState(resume=self._resume(), started=True)
        event = pi_schema.MessageEnd(
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": "err"}],
                "stopReason": "error",
                "errorMessage": "quota exceeded",
            }
        )
        translate_pi_event(event, title="pi", meta=self._meta(), state=state)
        assert state.last_assistant_error == "quota exceeded"

    def test_agent_end(self) -> None:
        state = PiStreamState(resume=self._resume(), started=True)
        event = pi_schema.AgentEnd(
            messages=[
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"total": 100},
                },
            ]
        )
        results = translate_pi_event(
            event, title="pi", meta=self._meta(), state=state
        )
        completed = [e for e in results if isinstance(e, CompletedEvent)]
        assert len(completed) == 1
        assert completed[0].ok is True
        assert completed[0].answer == "done"

    def test_agent_end_with_error(self) -> None:
        state = PiStreamState(resume=self._resume(), started=True)
        state.last_assistant_error = "quota exceeded"
        event = pi_schema.AgentEnd(messages=[])
        results = translate_pi_event(
            event, title="pi", meta=self._meta(), state=state
        )
        completed = [e for e in results if isinstance(e, CompletedEvent)]
        assert len(completed) == 1
        assert completed[0].ok is False
        assert completed[0].error == "quota exceeded"

    def test_unknown_event(self) -> None:
        state = PiStreamState(resume=self._resume(), started=True)
        event = pi_schema.AgentStart()
        results = translate_pi_event(
            event, title="pi", meta=self._meta(), state=state
        )
        # Should not produce action/completed events
        assert all(not isinstance(e, (ActionEvent, CompletedEvent)) for e in results)

    def test_tool_execution_start_file_change(self) -> None:
        state = PiStreamState(resume=self._resume(), started=True)
        event = pi_schema.ToolExecutionStart(
            toolCallId="tc2", toolName="edit", args={"path": "/tmp/foo.py"}
        )
        results = translate_pi_event(
            event, title="pi", meta=self._meta(), state=state
        )
        action_events = [e for e in results if isinstance(e, ActionEvent)]
        assert len(action_events) == 1
        assert action_events[0].action.kind == "file_change"
        detail = action_events[0].action.detail
        assert "changes" in detail


# ---------------------------------------------------------------------------
# PiRunner: command building
# ---------------------------------------------------------------------------


class TestPiRunner:
    def _runner(self, **kwargs: Any) -> PiRunner:
        defaults: dict[str, Any] = dict(
            extra_args=[],
            model=None,
            provider=None,
        )
        defaults.update(kwargs)
        return PiRunner(**defaults)

    def test_command(self) -> None:
        r = self._runner()
        assert r.command() == "pi"

    def test_build_args_basic(self) -> None:
        r = self._runner()
        resume = ResumeToken(engine="pi", value="sess-1")
        state = PiStreamState(resume=resume)
        args = r.build_args("hello world", resume, state=state)
        assert "--print" in args
        assert "--mode" in args
        assert "json" in args
        assert "--session" in args
        idx = args.index("--session")
        assert args[idx + 1] == "sess-1"
        assert "hello world" in args

    def test_build_args_with_model(self) -> None:
        r = self._runner(model="claude-opus-4-0520")
        resume = ResumeToken(engine="pi", value="s")
        state = PiStreamState(resume=resume)
        args = r.build_args("hi", resume, state=state)
        assert "--model" in args
        idx = args.index("--model")
        assert args[idx + 1] == "claude-opus-4-0520"

    def test_build_args_with_provider(self) -> None:
        r = self._runner(provider="anthropic")
        resume = ResumeToken(engine="pi", value="s")
        state = PiStreamState(resume=resume)
        args = r.build_args("hi", resume, state=state)
        assert "--provider" in args
        idx = args.index("--provider")
        assert args[idx + 1] == "anthropic"

    def test_build_args_extra_args(self) -> None:
        r = self._runner(extra_args=["--verbose", "--debug"])
        resume = ResumeToken(engine="pi", value="s")
        state = PiStreamState(resume=resume)
        args = r.build_args("hi", resume, state=state)
        assert "--verbose" in args
        assert "--debug" in args

    def test_build_args_run_options_override(self) -> None:
        r = self._runner(model="default-model")
        resume = ResumeToken(engine="pi", value="s")
        state = PiStreamState(resume=resume)
        tok = set_run_options(EngineRunOptions(model="override-model"))
        try:
            args = r.build_args("hi", resume, state=state)
            idx = args.index("--model")
            assert args[idx + 1] == "override-model"
        finally:
            reset_run_options(tok)

    def test_build_args_sanitize_prompt(self) -> None:
        r = self._runner()
        resume = ResumeToken(engine="pi", value="s")
        state = PiStreamState(resume=resume)
        args = r.build_args("-dangerous", resume, state=state)
        # Prompt starting with "-" gets space prepended
        assert " -dangerous" in args

    def test_format_resume(self) -> None:
        r = self._runner()
        token = ResumeToken(engine="pi", value="abc")
        assert r.format_resume(token) == "`pi --session abc`"

    def test_format_resume_wrong_engine(self) -> None:
        r = self._runner()
        token = ResumeToken(engine="gemini", value="abc")
        with pytest.raises(RuntimeError, match="engine"):
            r.format_resume(token)

    def test_format_resume_with_spaces(self) -> None:
        r = self._runner()
        token = ResumeToken(engine="pi", value="/path with spaces/file.jsonl")
        result = r.format_resume(token)
        assert '"/path with spaces/file.jsonl"' in result

    def test_format_resume_with_quotes(self) -> None:
        r = self._runner()
        token = ResumeToken(engine="pi", value='path"with"quotes')
        result = r.format_resume(token)
        assert '\\"' in result

    def test_stdin_payload_none(self) -> None:
        r = self._runner()
        resume = ResumeToken(engine="pi", value="s")
        state = PiStreamState(resume=resume)
        assert r.stdin_payload("hello", resume, state=state) is None

    def test_env_sets_no_color(self) -> None:
        r = self._runner()
        resume = ResumeToken(engine="pi", value="s")
        state = PiStreamState(resume=resume)
        env = r.env(state=state)
        assert env is not None
        assert env.get("NO_COLOR") == "1"
        assert env.get("CI") == "1"

    def test_new_state_with_resume(self) -> None:
        r = self._runner()
        resume = ResumeToken(engine="pi", value="s1")
        state = r.new_state("hello", resume)
        assert state.resume == resume
        assert state.allow_id_promotion is False

    def test_new_state_without_resume(self) -> None:
        r = self._runner()
        state = r.new_state("hello", None)
        assert state.resume.engine == "pi"
        assert state.resume.value.endswith(".jsonl")
        assert state.allow_id_promotion is True

    def test_extract_resume(self) -> None:
        r = self._runner()
        result = r.extract_resume("pi --session my-token")
        assert result is not None
        assert result.value == "my-token"

    def test_extract_resume_quoted(self) -> None:
        r = self._runner()
        result = r.extract_resume('pi --session "my token"')
        assert result is not None
        assert result.value == "my token"

    def test_extract_resume_none(self) -> None:
        r = self._runner()
        assert r.extract_resume(None) is None
        assert r.extract_resume("no match here") is None

    def test_process_error_events(self) -> None:
        r = self._runner()
        resume = ResumeToken(engine="pi", value="s")
        state = PiStreamState(resume=resume)
        events = r.process_error_events(
            2, resume=resume, found_session=None, state=state
        )
        assert len(events) == 2
        completed = events[1]
        assert isinstance(completed, CompletedEvent)
        assert completed.ok is False
        assert "rc=2" in completed.error

    def test_stream_end_events(self) -> None:
        r = self._runner()
        resume = ResumeToken(engine="pi", value="s")
        state = PiStreamState(resume=resume)
        events = r.stream_end_events(
            resume=resume, found_session=None, state=state
        )
        assert len(events) == 1
        completed = events[0]
        assert isinstance(completed, CompletedEvent)
        assert completed.ok is False
        assert "agent_end" in completed.error


# ---------------------------------------------------------------------------
# Pi build_runner
# ---------------------------------------------------------------------------


class TestPiBuildRunner:
    def test_build_runner_defaults(self) -> None:
        config: dict[str, Any] = {}
        runner = pi_build_runner(config, Path("/fake"))
        assert isinstance(runner, PiRunner)

    def test_build_runner_with_model(self) -> None:
        config: dict[str, Any] = {"model": "test-model"}
        runner = pi_build_runner(config, Path("/fake"))
        assert isinstance(runner, PiRunner)
        assert runner.model == "test-model"

    def test_build_runner_with_extra_args(self) -> None:
        config: dict[str, Any] = {"extra_args": ["--verbose"]}
        runner = pi_build_runner(config, Path("/fake"))
        assert isinstance(runner, PiRunner)
        assert runner.extra_args == ["--verbose"]

    def test_build_runner_invalid_extra_args(self) -> None:
        config: dict[str, Any] = {"extra_args": "not-a-list"}
        with pytest.raises(ConfigError):
            pi_build_runner(config, Path("/fake"))

    def test_build_runner_invalid_model(self) -> None:
        config: dict[str, Any] = {"model": 123}
        with pytest.raises(ConfigError):
            pi_build_runner(config, Path("/fake"))

    def test_build_runner_invalid_provider(self) -> None:
        config: dict[str, Any] = {"provider": 123}
        with pytest.raises(ConfigError):
            pi_build_runner(config, Path("/fake"))

    def test_build_runner_with_provider(self) -> None:
        config: dict[str, Any] = {"provider": "openai"}
        runner = pi_build_runner(config, Path("/fake"))
        assert isinstance(runner, PiRunner)
        assert runner.provider == "openai"


# ---------------------------------------------------------------------------
# Pi: _default_session_dir
# ---------------------------------------------------------------------------


class TestDefaultSessionDir:
    def test_default_session_dir(self) -> None:
        from pathlib import PurePosixPath

        result = _default_session_dir(PurePosixPath("/home/user/project"))
        assert "sessions" in str(result)
        assert "--home-user-project--" in str(result)

    def test_default_session_dir_with_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pathlib import PurePosixPath

        monkeypatch.setenv("PI_CODING_AGENT_DIR", "/custom/agent")
        result = _default_session_dir(PurePosixPath("/home/user/project"))
        assert str(result).startswith("/custom/agent")
