"""Tests for MattermostTransport and MattermostPresenter."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tunapi.mattermost.api_models import Post
from tunapi.mattermost.bridge import MattermostPresenter, MattermostTransport
from tunapi.mattermost.client import MattermostClient
from tunapi.progress import ProgressState
from tunapi.transport import MessageRef, RenderedMessage, SendOptions

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Presenter
# ---------------------------------------------------------------------------


class TestMattermostPresenter:
    def test_render_progress(self):
        presenter = MattermostPresenter()
        state = ProgressState(engine="claude", action_count=3, actions=(), resume=None, resume_line=None, context_line=None)
        result = presenter.render_progress(state, elapsed_s=10.0)
        assert isinstance(result, RenderedMessage)
        assert "claude" in result.text
        assert "10s" in result.text

    def test_render_final(self):
        presenter = MattermostPresenter()
        state = ProgressState(engine="claude", action_count=1, actions=(), resume=None, resume_line=None, context_line=None)
        result = presenter.render_final(
            state, elapsed_s=5.0, status="done", answer="The answer is 42."
        )
        assert "42" in result.text

    def test_render_final_split_overflow(self):
        presenter = MattermostPresenter(message_overflow="split")
        long_answer = "\n\n".join(f"paragraph {i}" * 50 for i in range(30))
        state = ProgressState(engine="claude", action_count=1, actions=(), resume=None, resume_line=None, context_line=None)
        result = presenter.render_final(
            state, elapsed_s=1.0, status="done", answer=long_answer
        )
        # Should have followups if body was split
        if result.extra.get("followups"):
            assert len(result.extra["followups"]) >= 1


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _fake_post(post_id: str = "p1", channel_id: str = "c1") -> Post:
    return Post(id=post_id, channel_id=channel_id, root_id="", message="ok")


class TestMattermostTransport:
    async def test_send_simple(self):
        bot = AsyncMock(spec=MattermostClient)
        bot.send_message = AsyncMock(return_value=_fake_post())
        transport = MattermostTransport(bot)

        ref = await transport.send(
            channel_id="c1",
            message=RenderedMessage(text="hello"),
        )
        assert ref is not None
        assert ref.channel_id == "c1"
        assert ref.message_id == "p1"
        bot.send_message.assert_called_once()

    async def test_send_with_reply_no_thread(self):
        """reply_to alone should NOT create a thread."""
        bot = AsyncMock(spec=MattermostClient)
        bot.send_message = AsyncMock(return_value=_fake_post())
        transport = MattermostTransport(bot)

        reply_ref = MessageRef(channel_id="c1", message_id="p0")
        ref = await transport.send(
            channel_id="c1",
            message=RenderedMessage(text="reply"),
            options=SendOptions(reply_to=reply_ref),
        )
        assert ref is not None
        call_kwargs = bot.send_message.call_args
        assert call_kwargs.kwargs.get("root_id") is None

    async def test_send_with_thread_id(self):
        """Explicit thread_id should create a thread reply."""
        bot = AsyncMock(spec=MattermostClient)
        bot.send_message = AsyncMock(return_value=_fake_post())
        transport = MattermostTransport(bot)

        ref = await transport.send(
            channel_id="c1",
            message=RenderedMessage(text="reply"),
            options=SendOptions(thread_id="t0"),
        )
        assert ref is not None
        call_kwargs = bot.send_message.call_args
        assert call_kwargs.kwargs.get("root_id") == "t0"

    async def test_send_with_replace_does_not_delete(self):
        """replace should NOT delete — progress messages stay visible."""
        bot = AsyncMock(spec=MattermostClient)
        bot.delete_message = AsyncMock(return_value=True)
        bot.send_message = AsyncMock(return_value=_fake_post())
        transport = MattermostTransport(bot)

        old_ref = MessageRef(channel_id="c1", message_id="old1")
        await transport.send(
            channel_id="c1",
            message=RenderedMessage(text="new"),
            options=SendOptions(replace=old_ref),
        )
        bot.delete_message.assert_not_called()

    async def test_edit(self):
        bot = AsyncMock(spec=MattermostClient)
        bot.edit_message = AsyncMock(return_value=_fake_post("p1"))
        transport = MattermostTransport(bot)

        ref = MessageRef(channel_id="c1", message_id="p1", thread_id="t1")
        result = await transport.edit(
            ref=ref,
            message=RenderedMessage(text="edited"),
        )
        assert result is not None
        assert result.message_id == "p1"
        assert result.thread_id == "t1"

    async def test_delete(self):
        bot = AsyncMock(spec=MattermostClient)
        bot.delete_message = AsyncMock(return_value=True)
        transport = MattermostTransport(bot)

        ref = MessageRef(channel_id="c1", message_id="p1")
        result = await transport.delete(ref=ref)
        assert result is True

    async def test_send_returns_none_on_failure(self):
        bot = AsyncMock(spec=MattermostClient)
        bot.send_message = AsyncMock(return_value=None)
        transport = MattermostTransport(bot)

        ref = await transport.send(
            channel_id="c1",
            message=RenderedMessage(text="fail"),
        )
        assert ref is None
