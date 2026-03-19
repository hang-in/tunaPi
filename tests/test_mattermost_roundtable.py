"""Tests for roundtable pure-logic functions, in-memory store, and async flows."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from tunapi.mattermost.roundtable import (
    RoundtableSession,
    RoundtableStore,
    _MAX_ANSWER_LENGTH,
    _build_round_prompt,
    parse_followup_args,
    parse_rt_args,
    run_followup_round,
    run_roundtable,
)
from tunapi.transport import MessageRef, RenderedMessage, SendOptions
from tunapi.transport_runtime import RoundtableConfig

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_RT_CONFIG = RoundtableConfig(engines=(), rounds=1, max_rounds=3)


def _make_session(
    thread_id: str = "t1",
    channel_id: str = "c1",
    engines: list[str] | None = None,
    total_rounds: int = 1,
) -> RoundtableSession:
    return RoundtableSession(
        thread_id=thread_id,
        channel_id=channel_id,
        topic="test topic",
        engines=engines or ["claude", "gemini"],
        total_rounds=total_rounds,
    )


# ---------------------------------------------------------------------------
# RoundtableStore
# ---------------------------------------------------------------------------


class TestRoundtableStore:
    def test_put_and_get(self):
        store = RoundtableStore()
        session = _make_session()
        store.put(session)
        assert store.get("t1") is session

    def test_get_returns_none_for_unknown(self):
        store = RoundtableStore()
        assert store.get("unknown") is None

    def test_complete_marks_session(self):
        store = RoundtableStore()
        session = _make_session()
        store.put(session)
        assert not session.completed
        store.complete("t1")
        assert session.completed

    def test_get_completed_returns_none_for_active(self):
        store = RoundtableStore()
        store.put(_make_session())
        assert store.get_completed("t1") is None

    def test_get_completed_returns_session_for_completed(self):
        store = RoundtableStore()
        session = _make_session()
        store.put(session)
        store.complete("t1")
        assert store.get_completed("t1") is session

    def test_remove(self):
        store = RoundtableStore()
        session = _make_session()
        store.put(session)
        removed = store.remove("t1")
        assert removed is session
        assert store.get("t1") is None

    def test_completed_sessions_persist(self):
        """Completed sessions are kept indefinitely (no TTL)."""
        store = RoundtableStore()
        session = _make_session()
        store.put(session)
        store.complete("t1")
        # Should still be accessible
        assert store.get("t1") is session
        assert store.get_completed("t1") is session

    def test_remove_deletes_completed_session(self):
        """!rt close removes completed sessions explicitly."""
        store = RoundtableStore()
        session = _make_session()
        store.put(session)
        store.complete("t1")
        store.remove("t1")
        assert store.get("t1") is None

    def test_active_sessions_accessible(self):
        """Active (not completed) sessions are accessible via get()."""
        store = RoundtableStore()
        session = _make_session()
        store.put(session)
        assert store.get("t1") is session
        assert store.get_completed("t1") is None  # not completed yet


# ---------------------------------------------------------------------------
# parse_rt_args
# ---------------------------------------------------------------------------


class TestParseRtArgs:
    def test_simple_topic(self):
        topic, rounds, err = parse_rt_args("리팩토링 논의", _DEFAULT_RT_CONFIG)
        assert topic == "리팩토링 논의"
        assert rounds == 1
        assert err is None

    def test_quoted_topic(self):
        topic, rounds, err = parse_rt_args('"multi word topic"', _DEFAULT_RT_CONFIG)
        assert topic == "multi word topic"
        assert rounds == 1
        assert err is None

    def test_rounds_flag(self):
        topic, rounds, err = parse_rt_args('"topic" --rounds 2', _DEFAULT_RT_CONFIG)
        assert topic == "topic"
        assert rounds == 2
        assert err is None

    def test_rounds_exceeds_max(self):
        _, _, err = parse_rt_args('"topic" --rounds 10', _DEFAULT_RT_CONFIG)
        assert err is not None
        assert "3" in err  # max_rounds=3

    def test_rounds_zero(self):
        _, _, err = parse_rt_args('"topic" --rounds 0', _DEFAULT_RT_CONFIG)
        assert err is not None
        assert "1" in err  # at least 1

    def test_invalid_rounds(self):
        _, _, err = parse_rt_args('"topic" --rounds abc', _DEFAULT_RT_CONFIG)
        assert err is not None
        assert "abc" in err

    def test_empty_args(self):
        topic, rounds, err = parse_rt_args("", _DEFAULT_RT_CONFIG)
        assert topic == ""
        assert rounds == 0
        assert err is None  # show usage

    def test_parse_error(self):
        _, _, err = parse_rt_args('"unclosed quote', _DEFAULT_RT_CONFIG)
        assert err is not None
        assert "Parse error" in err


# ---------------------------------------------------------------------------
# parse_followup_args
# ---------------------------------------------------------------------------


_AVAILABLE_ENGINES = ["claude", "gemini", "codex"]


class TestParseFollowupArgs:
    def test_topic_only(self):
        topic, engines, err = parse_followup_args("새 질문", _AVAILABLE_ENGINES)
        assert topic == "새 질문"
        assert engines is None
        assert err is None

    def test_engine_filter_and_topic(self):
        topic, engines, err = parse_followup_args(
            "claude,gemini 새 질문", _AVAILABLE_ENGINES
        )
        assert topic == "새 질문"
        assert engines == ["claude", "gemini"]
        assert err is None

    def test_unknown_engine_treated_as_topic(self):
        topic, engines, err = parse_followup_args(
            "unknown 새 질문", _AVAILABLE_ENGINES
        )
        assert topic == "unknown 새 질문"
        assert engines is None
        assert err is None

    def test_partial_engine_match(self):
        topic, engines, err = parse_followup_args(
            "claude,unknown topic", _AVAILABLE_ENGINES
        )
        assert topic == "claude,unknown topic"
        assert engines is None
        assert err is None

    def test_empty_args(self):
        topic, engines, err = parse_followup_args("", _AVAILABLE_ENGINES)
        assert topic == ""
        assert err is None

    def test_case_insensitive_engine(self):
        topic, engines, err = parse_followup_args(
            "Claude 새 질문", _AVAILABLE_ENGINES
        )
        assert topic == "새 질문"
        assert engines == ["claude"]
        assert err is None


# ---------------------------------------------------------------------------
# _build_round_prompt
# ---------------------------------------------------------------------------


class TestBuildRoundPrompt:
    def test_no_context(self):
        result = _build_round_prompt("test topic", [], 1)
        assert result == "test topic"

    def test_with_previous_rounds(self):
        transcript = [("claude", "answer1"), ("gemini", "answer2")]
        result = _build_round_prompt("topic", transcript, 2)
        assert "이전 라운드 응답" in result
        assert "**[claude]**" in result
        assert "answer1" in result

    def test_with_current_round_responses(self):
        result = _build_round_prompt(
            "topic",
            [],
            1,
            current_round_responses=[("claude", "first answer")],
        )
        assert "이번 라운드 다른 에이전트 답변" in result
        assert "**[claude]**" in result

    def test_long_answer_truncated(self):
        long_answer = "x" * (_MAX_ANSWER_LENGTH + 100)
        transcript = [("claude", long_answer)]
        result = _build_round_prompt("topic", transcript, 2)
        assert "..." in result
        # Should not contain full answer
        assert long_answer not in result

    def test_both_previous_and_current(self):
        result = _build_round_prompt(
            "topic",
            [("claude", "prev")],
            2,
            current_round_responses=[("gemini", "curr")],
        )
        assert "이전 라운드 응답" in result
        assert "이번 라운드 다른 에이전트 답변" in result
        assert "---" in result  # separator


# ---------------------------------------------------------------------------
# Async integration tests: run_roundtable / run_followup_round
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Minimal transport that records send calls."""

    def __init__(self) -> None:
        self._next_id = 1
        self.sent: list[str] = []

    async def send(
        self,
        *,
        channel_id: int | str,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef:
        self.sent.append(message.text)
        ref = MessageRef(channel_id=channel_id, message_id=self._next_id)
        self._next_id += 1
        return ref

    async def edit(self, *, ref, message, wait=True):
        return ref

    async def delete(self, *, ref):
        return True

    async def close(self):
        pass


@dataclass
class _FakeResolvedRunner:
    engine: str
    runner: object
    available: bool = True
    issue: str | None = None


class _FakeRuntime:
    """Minimal runtime stub for roundtable tests."""

    def __init__(self, *, engines: list[str], fail_engine: str | None = None):
        self._engines = set(engines)
        self._fail_engine = fail_engine

    def resolve_runner(self, *, resume_token, engine_override):
        if engine_override == self._fail_engine:
            return _FakeResolvedRunner(
                engine=engine_override, runner=None, available=False,
                issue=f"{engine_override} unavailable",
            )
        return _FakeResolvedRunner(engine=engine_override, runner=object())

    def resolve_run_cwd(self, context):
        return None

    def format_context_line(self, context):
        return ""


def _make_cfg(transport, runtime, answers: dict[str, str] | None = None):
    """Build a minimal MattermostBridgeConfig-like object for testing."""
    answers = answers or {}

    @dataclass
    class FakeExecCfg:
        transport: object
        presenter: object = None
        final_notify: bool = False

    @dataclass
    class FakeCfg:
        runtime: object
        exec_cfg: object

    return FakeCfg(runtime=runtime, exec_cfg=FakeExecCfg(transport=transport))


class TestRunRoundtable:
    async def test_single_round_collects_transcript(self):
        transport = _FakeTransport()
        runtime = _FakeRuntime(engines=["claude", "gemini"])
        cfg = _make_cfg(transport, runtime)

        session = _make_session(engines=["claude", "gemini"])

        with patch(
            "tunapi.mattermost.roundtable.handle_message",
            new_callable=AsyncMock,
            side_effect=["claude answer", "gemini answer"],
        ):
            await run_roundtable(
                session, cfg=cfg, chat_prefs=None,
                running_tasks={}, ambient_context=None,
            )

        assert session.transcript == [
            ("claude", "claude answer"),
            ("gemini", "gemini answer"),
        ]
        assert any("완료" in t for t in transport.sent)

    async def test_multi_round_sends_round_headers(self):
        transport = _FakeTransport()
        runtime = _FakeRuntime(engines=["claude"])
        cfg = _make_cfg(transport, runtime)

        session = RoundtableSession(
            thread_id="t1", channel_id="c1",
            topic="topic", engines=["claude"], total_rounds=2,
        )

        with patch(
            "tunapi.mattermost.roundtable.handle_message",
            new_callable=AsyncMock,
            side_effect=["r1 answer", "r2 answer"],
        ):
            await run_roundtable(
                session, cfg=cfg, chat_prefs=None,
                running_tasks={}, ambient_context=None,
            )

        assert len(session.transcript) == 2
        round_headers = [t for t in transport.sent if "Round" in t and "---" in t]
        assert len(round_headers) == 2

    async def test_cancel_stops_roundtable(self):
        transport = _FakeTransport()
        runtime = _FakeRuntime(engines=["claude", "gemini"])
        cfg = _make_cfg(transport, runtime)

        session = _make_session(engines=["claude", "gemini"])
        session.cancel_event.set()  # pre-cancelled

        with patch(
            "tunapi.mattermost.roundtable.handle_message",
            new_callable=AsyncMock,
        ) as mock_hm:
            await run_roundtable(
                session, cfg=cfg, chat_prefs=None,
                running_tasks={}, ambient_context=None,
            )

        mock_hm.assert_not_called()
        assert any("cancelled" in t for t in transport.sent)

    async def test_unavailable_engine_skipped(self):
        transport = _FakeTransport()
        runtime = _FakeRuntime(engines=["claude", "gemini"], fail_engine="gemini")
        cfg = _make_cfg(transport, runtime)

        session = _make_session(engines=["claude", "gemini"])

        with patch(
            "tunapi.mattermost.roundtable.handle_message",
            new_callable=AsyncMock,
            return_value="claude answer",
        ):
            await run_roundtable(
                session, cfg=cfg, chat_prefs=None,
                running_tasks={}, ambient_context=None,
            )

        # Only claude's answer in transcript
        assert len(session.transcript) == 1
        assert session.transcript[0] == ("claude", "claude answer")
        # Warning sent for gemini
        assert any("gemini" in t and "⚠️" in t for t in transport.sent)

    async def test_handle_message_error_skips_engine(self):
        transport = _FakeTransport()
        runtime = _FakeRuntime(engines=["claude", "gemini"])
        cfg = _make_cfg(transport, runtime)

        session = _make_session(engines=["claude", "gemini"])

        with patch(
            "tunapi.mattermost.roundtable.handle_message",
            new_callable=AsyncMock,
            side_effect=[RuntimeError("boom"), "gemini ok"],
        ):
            await run_roundtable(
                session, cfg=cfg, chat_prefs=None,
                running_tasks={}, ambient_context=None,
            )

        # claude errored, gemini succeeded
        assert len(session.transcript) == 1
        assert session.transcript[0] == ("gemini", "gemini ok")
        assert any("claude" in t and "error" in t.lower() for t in transport.sent)


class TestRunFollowupRound:
    async def test_followup_appends_to_transcript(self):
        transport = _FakeTransport()
        runtime = _FakeRuntime(engines=["claude", "gemini"])
        cfg = _make_cfg(transport, runtime)

        session = _make_session(engines=["claude", "gemini"])
        session.transcript = [("claude", "original")]
        session.completed = True
        session.current_round = 1

        with patch(
            "tunapi.mattermost.roundtable.handle_message",
            new_callable=AsyncMock,
            side_effect=["follow claude", "follow gemini"],
        ):
            await run_followup_round(
                session, "followup topic", None,
                cfg=cfg, running_tasks={}, ambient_context=None,
            )

        assert session.current_round == 2
        assert session.completed is True
        assert len(session.transcript) == 3  # original + 2 followup
        assert any("Follow-up" in t for t in transport.sent)
        assert any("완료" in t for t in transport.sent)

    async def test_followup_filters_engines(self):
        transport = _FakeTransport()
        runtime = _FakeRuntime(engines=["claude", "gemini"])
        cfg = _make_cfg(transport, runtime)

        session = _make_session(engines=["claude", "gemini"])
        session.completed = True
        session.current_round = 1

        with patch(
            "tunapi.mattermost.roundtable.handle_message",
            new_callable=AsyncMock,
            return_value="claude only",
        ):
            await run_followup_round(
                session, "topic", ["claude"],
                cfg=cfg, running_tasks={}, ambient_context=None,
            )

        assert len(session.transcript) == 1
        assert session.transcript[0] == ("claude", "claude only")
