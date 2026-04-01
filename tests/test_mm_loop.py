"""Tests for testable helper functions in tunapi.mattermost.loop."""

from __future__ import annotations

from unittest.mock import AsyncMock

import anyio
import pytest

from tunapi.core.chat_prefs import Persona
from tunapi.core.roundtable import RoundtableSession, RoundtableStore
from tunapi.mattermost.bridge import CANCEL_EMOJI
from tunapi.mattermost.loop import (
    _PERSONA_PREFIX_RE,
    _ResolvedPrompt,
    _handle_cancel_reaction,
    _resolve_persona_prefix,
)
from tunapi.mattermost.types import (
    MattermostIncomingMessage,
    MattermostReactionEvent,
)
from tunapi.runner_bridge import RunningTask
from tunapi.transport import MessageRef


# ---------------------------------------------------------------------------
# _PERSONA_PREFIX_RE tests
# ---------------------------------------------------------------------------


class TestPersonaPrefixRegex:
    def test_matches_simple(self):
        m = _PERSONA_PREFIX_RE.match("@reviewer please check")
        assert m is not None
        assert m.group(1) == "reviewer"

    def test_matches_underscore_name(self):
        m = _PERSONA_PREFIX_RE.match("@code_reviewer check this")
        assert m is not None
        assert m.group(1) == "code_reviewer"

    def test_no_match_without_at(self):
        m = _PERSONA_PREFIX_RE.match("reviewer please check")
        assert m is None

    def test_no_match_without_space_after(self):
        m = _PERSONA_PREFIX_RE.match("@reviewer")
        assert m is None

    def test_no_match_midstring(self):
        m = _PERSONA_PREFIX_RE.match("hello @reviewer check")
        assert m is None


# ---------------------------------------------------------------------------
# _ResolvedPrompt tests
# ---------------------------------------------------------------------------


class TestResolvedPrompt:
    def test_basic(self):
        rp = _ResolvedPrompt(text="hello world", file_context="")
        assert rp.text == "hello world"
        assert rp.file_context == ""

    def test_with_file_context(self):
        rp = _ResolvedPrompt(text="analyse", file_context="[paths]")
        assert rp.file_context == "[paths]"


# ---------------------------------------------------------------------------
# MattermostIncomingMessage property tests
# ---------------------------------------------------------------------------


class TestMattermostIncomingMessage:
    def test_is_direct(self):
        msg = MattermostIncomingMessage(channel_type="D")
        assert msg.is_direct is True

    def test_is_not_direct(self):
        msg = MattermostIncomingMessage(channel_type="O")
        assert msg.is_direct is False

    def test_is_thread_reply(self):
        msg = MattermostIncomingMessage(root_id="root123")
        assert msg.is_thread_reply is True

    def test_is_not_thread_reply(self):
        msg = MattermostIncomingMessage(root_id="")
        assert msg.is_thread_reply is False


# ---------------------------------------------------------------------------
# _resolve_persona_prefix tests
# ---------------------------------------------------------------------------


class TestResolvePersonaPrefix:
    @pytest.fixture()
    def chat_prefs(self):
        return AsyncMock()

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

    @pytest.mark.anyio()
    async def test_prepends_persona_prompt(self, chat_prefs):
        chat_prefs.get_persona.return_value = Persona(
            name="architect", prompt="You are a software architect."
        )
        result = await _resolve_persona_prefix(
            "@architect design a system", chat_prefs
        )
        assert result is not None
        assert "[역할: architect]" in result
        assert "You are a software architect." in result
        assert "design a system" in result

    @pytest.mark.anyio()
    async def test_persona_name_lowered(self, chat_prefs):
        chat_prefs.get_persona.return_value = None
        await _resolve_persona_prefix("@Architect review", chat_prefs)
        chat_prefs.get_persona.assert_called_once_with("architect")

    @pytest.mark.anyio()
    async def test_user_text_preserved_after_prefix(self, chat_prefs):
        chat_prefs.get_persona.return_value = Persona(
            name="dev", prompt="Be brief."
        )
        result = await _resolve_persona_prefix("@dev fix the bug now", chat_prefs)
        assert result is not None
        assert result.endswith("fix the bug now")


# ---------------------------------------------------------------------------
# _handle_cancel_reaction tests
# ---------------------------------------------------------------------------


class TestHandleCancelReaction:
    @pytest.mark.anyio()
    async def test_ignores_non_cancel_emoji(self):
        reaction = MattermostReactionEvent(
            channel_id="ch1",
            post_id="p1",
            user_id="u1",
            emoji_name="thumbsup",
        )
        running_tasks = {}
        await _handle_cancel_reaction(reaction, running_tasks)
        # Should silently return

    @pytest.mark.anyio()
    async def test_cancels_matching_running_task(self):
        ref = MessageRef(channel_id="ch1", message_id="post123")
        task = RunningTask()
        running_tasks = {ref: task}
        assert not task.cancel_requested.is_set()

        reaction = MattermostReactionEvent(
            channel_id="ch1",
            post_id="post123",
            user_id="u1",
            emoji_name=CANCEL_EMOJI,
        )
        await _handle_cancel_reaction(reaction, running_tasks)
        assert task.cancel_requested.is_set()

    @pytest.mark.anyio()
    async def test_does_not_cancel_non_matching_task(self):
        ref = MessageRef(channel_id="ch1", message_id="other_post")
        task = RunningTask()
        running_tasks = {ref: task}

        reaction = MattermostReactionEvent(
            channel_id="ch1",
            post_id="post123",
            user_id="u1",
            emoji_name=CANCEL_EMOJI,
        )
        await _handle_cancel_reaction(reaction, running_tasks)
        assert not task.cancel_requested.is_set()

    @pytest.mark.anyio()
    async def test_cancels_roundtable_session(self):
        rt_store = RoundtableStore()
        session = RoundtableSession(
            thread_id="post123",
            channel_id="ch1",
            topic="design review",
            engines=["claude", "gemini"],
            total_rounds=2,
        )
        rt_store.put(session)
        assert not session.cancel_event.is_set()

        reaction = MattermostReactionEvent(
            channel_id="ch1",
            post_id="post123",
            user_id="u1",
            emoji_name=CANCEL_EMOJI,
        )
        await _handle_cancel_reaction(reaction, {}, roundtables=rt_store)
        assert session.cancel_event.is_set()

    @pytest.mark.anyio()
    async def test_roundtable_cancel_takes_priority_over_task(self):
        rt_store = RoundtableStore()
        session = RoundtableSession(
            thread_id="post123",
            channel_id="ch1",
            topic="test",
            engines=["claude"],
            total_rounds=1,
        )
        rt_store.put(session)

        ref = MessageRef(channel_id="ch1", message_id="post123")
        task = RunningTask()
        running_tasks = {ref: task}

        reaction = MattermostReactionEvent(
            channel_id="ch1",
            post_id="post123",
            user_id="u1",
            emoji_name=CANCEL_EMOJI,
        )
        await _handle_cancel_reaction(reaction, running_tasks, roundtables=rt_store)
        assert session.cancel_event.is_set()
        assert not task.cancel_requested.is_set()

    @pytest.mark.anyio()
    async def test_empty_running_tasks_no_error(self):
        reaction = MattermostReactionEvent(
            channel_id="ch1",
            post_id="post123",
            user_id="u1",
            emoji_name=CANCEL_EMOJI,
        )
        await _handle_cancel_reaction(reaction, {})
        # No error raised

    @pytest.mark.anyio()
    async def test_multiple_tasks_cancels_correct_one(self):
        ref1 = MessageRef(channel_id="ch1", message_id="post1")
        ref2 = MessageRef(channel_id="ch1", message_id="post2")
        task1 = RunningTask()
        task2 = RunningTask()
        running_tasks = {ref1: task1, ref2: task2}

        reaction = MattermostReactionEvent(
            channel_id="ch1",
            post_id="post2",
            user_id="u1",
            emoji_name=CANCEL_EMOJI,
        )
        await _handle_cancel_reaction(reaction, running_tasks)
        assert not task1.cancel_requested.is_set()
        assert task2.cancel_requested.is_set()
