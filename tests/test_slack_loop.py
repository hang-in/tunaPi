"""Tests for testable helper functions in tunapi.slack.loop."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from tunapi.core.chat_prefs import Persona
from tunapi.core.roundtable import RoundtableSession, RoundtableStore
from tunapi.runner_bridge import RunningTask
from tunapi.slack.bridge import CANCEL_EMOJI
from tunapi.slack.loop import (
    _PERSONA_PREFIX_RE,
    _ResolvedPrompt,
    _handle_cancel_reaction,
    _resolve_persona_prefix,
)
from tunapi.slack.parsing import SlackMessageEvent, SlackReactionEvent
from tunapi.transport import MessageRef


# ---------------------------------------------------------------------------
# _PERSONA_PREFIX_RE tests
# ---------------------------------------------------------------------------


class TestPersonaPrefixRegex:
    def test_matches_simple(self):
        m = _PERSONA_PREFIX_RE.match("@reviewer please check")
        assert m is not None
        assert m.group(1) == "reviewer"

    def test_matches_unicode_name(self):
        m = _PERSONA_PREFIX_RE.match("@리뷰어 check this")
        assert m is not None
        assert m.group(1) == "리뷰어"

    def test_no_match_without_at(self):
        m = _PERSONA_PREFIX_RE.match("reviewer please check")
        assert m is None

    def test_no_match_without_space_after(self):
        m = _PERSONA_PREFIX_RE.match("@reviewer")
        assert m is None

    def test_no_match_midstring(self):
        m = _PERSONA_PREFIX_RE.match("hello @reviewer check")
        assert m is None

    def test_captures_only_first_word(self):
        m = _PERSONA_PREFIX_RE.match("@abc def ghi")
        assert m is not None
        assert m.group(1) == "abc"


# ---------------------------------------------------------------------------
# _ResolvedPrompt tests
# ---------------------------------------------------------------------------


class TestResolvedPrompt:
    def test_basic(self):
        rp = _ResolvedPrompt(text="hello", file_context="")
        assert rp.text == "hello"
        assert rp.file_context == ""

    def test_with_file_context(self):
        rp = _ResolvedPrompt(text="analyse this", file_context="[file: /tmp/a.py]")
        assert rp.file_context == "[file: /tmp/a.py]"


# ---------------------------------------------------------------------------
# _resolve_persona_prefix tests
# ---------------------------------------------------------------------------


class TestResolvePersonaPrefix:
    @pytest.fixture()
    def chat_prefs(self):
        prefs = AsyncMock()
        return prefs

    @pytest.mark.anyio()
    async def test_returns_none_when_no_prefix(self, chat_prefs):
        result = await _resolve_persona_prefix("just a question", chat_prefs)
        assert result is None
        chat_prefs.get_persona.assert_not_called()

    @pytest.mark.anyio()
    async def test_returns_none_when_persona_not_found(self, chat_prefs):
        chat_prefs.get_persona.return_value = None
        result = await _resolve_persona_prefix("@unknown tell me", chat_prefs)
        assert result is None
        chat_prefs.get_persona.assert_called_once_with("unknown")

    @pytest.mark.anyio()
    async def test_prepends_persona_prompt(self, chat_prefs):
        chat_prefs.get_persona.return_value = Persona(
            name="critic", prompt="You are a harsh critic."
        )
        result = await _resolve_persona_prefix("@critic review my code", chat_prefs)
        assert result is not None
        assert "[역할: critic]" in result
        assert "You are a harsh critic." in result
        assert "review my code" in result

    @pytest.mark.anyio()
    async def test_persona_name_is_lowered(self, chat_prefs):
        chat_prefs.get_persona.return_value = None
        await _resolve_persona_prefix("@CriTiC review", chat_prefs)
        chat_prefs.get_persona.assert_called_once_with("critic")


# ---------------------------------------------------------------------------
# _handle_cancel_reaction tests
# ---------------------------------------------------------------------------


class TestHandleCancelReaction:
    @pytest.mark.anyio()
    async def test_ignores_non_cancel_emoji(self):
        reaction = SlackReactionEvent(
            channel_id="C1",
            user_id="U1",
            emoji="thumbsup",
            ts="1234.5",
            item_ts="9999.0",
        )
        running_tasks = {}
        await _handle_cancel_reaction(reaction, running_tasks)
        # Should not raise or modify anything

    @pytest.mark.anyio()
    async def test_cancels_matching_running_task(self):
        ref = MessageRef(channel_id="C1", message_id="9999.0")
        task = RunningTask()
        running_tasks = {ref: task}
        assert not task.cancel_requested.is_set()

        reaction = SlackReactionEvent(
            channel_id="C1",
            user_id="U1",
            emoji=CANCEL_EMOJI,
            ts="1234.5",
            item_ts="9999.0",
        )
        await _handle_cancel_reaction(reaction, running_tasks)
        assert task.cancel_requested.is_set()

    @pytest.mark.anyio()
    async def test_does_not_cancel_non_matching_task(self):
        ref = MessageRef(channel_id="C1", message_id="1111.0")
        task = RunningTask()
        running_tasks = {ref: task}

        reaction = SlackReactionEvent(
            channel_id="C1",
            user_id="U1",
            emoji=CANCEL_EMOJI,
            ts="1234.5",
            item_ts="9999.0",
        )
        await _handle_cancel_reaction(reaction, running_tasks)
        assert not task.cancel_requested.is_set()

    @pytest.mark.anyio()
    async def test_cancels_roundtable_session(self):
        rt_store = RoundtableStore()
        session = RoundtableSession(
            thread_id="9999.0",
            channel_id="C1",
            topic="test topic",
            engines=["claude"],
            total_rounds=1,
        )
        rt_store.put(session)
        assert not session.cancel_event.is_set()

        reaction = SlackReactionEvent(
            channel_id="C1",
            user_id="U1",
            emoji=CANCEL_EMOJI,
            ts="1234.5",
            item_ts="9999.0",
        )
        await _handle_cancel_reaction(reaction, {}, roundtables=rt_store)
        assert session.cancel_event.is_set()

    @pytest.mark.anyio()
    async def test_roundtable_cancel_takes_priority_over_task(self):
        """When both a roundtable session and a running task match,
        the roundtable cancel takes priority (function returns early)."""
        rt_store = RoundtableStore()
        session = RoundtableSession(
            thread_id="9999.0",
            channel_id="C1",
            topic="test",
            engines=["claude"],
            total_rounds=1,
        )
        rt_store.put(session)

        ref = MessageRef(channel_id="C1", message_id="9999.0")
        task = RunningTask()
        running_tasks = {ref: task}

        reaction = SlackReactionEvent(
            channel_id="C1",
            user_id="U1",
            emoji=CANCEL_EMOJI,
            ts="1234.5",
            item_ts="9999.0",
        )
        await _handle_cancel_reaction(reaction, running_tasks, roundtables=rt_store)
        assert session.cancel_event.is_set()
        assert not task.cancel_requested.is_set()

    @pytest.mark.anyio()
    async def test_empty_running_tasks(self):
        reaction = SlackReactionEvent(
            channel_id="C1",
            user_id="U1",
            emoji=CANCEL_EMOJI,
            ts="1234.5",
            item_ts="9999.0",
        )
        await _handle_cancel_reaction(reaction, {})
        # No error, nothing to cancel
