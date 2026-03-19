"""Tests for roundtable pure-logic functions and in-memory store."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tunapi.mattermost.roundtable import (
    RoundtableSession,
    RoundtableStore,
    _MAX_ANSWER_LENGTH,
    _SESSION_TTL_SECONDS,
    _build_round_prompt,
    parse_followup_args,
    parse_rt_args,
)
from tunapi.transport_runtime import RoundtableConfig

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_RT_CONFIG = RoundtableConfig(engines=(), rounds=1, max_rounds=3)


def _make_session(thread_id: str = "t1", channel_id: str = "c1") -> RoundtableSession:
    return RoundtableSession(
        thread_id=thread_id,
        channel_id=channel_id,
        topic="test topic",
        engines=["claude", "gemini"],
        total_rounds=1,
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

    def test_evict_expired_sessions(self):
        store = RoundtableStore()
        session = _make_session()
        store.put(session)
        store.complete("t1")
        # Simulate time passing beyond TTL
        with patch("tunapi.mattermost.roundtable.time.monotonic") as mock_mono:
            # First call: complete timestamp (already set), next calls: eviction check
            mock_mono.return_value = store._completed_at["t1"] + _SESSION_TTL_SECONDS + 1
            assert store.get("t1") is None

    def test_evict_keeps_active_sessions(self):
        store = RoundtableStore()
        session = _make_session()
        store.put(session)
        # Active (not completed) sessions should survive regardless of time
        with patch("tunapi.mattermost.roundtable.time.monotonic") as mock_mono:
            mock_mono.return_value = 999999999.0
            assert store.get("t1") is session


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
