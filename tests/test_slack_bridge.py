from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest

from tunapi.transport import MessageRef, RenderedMessage, SendOptions
from tunapi.slack.bridge import SlackPresenter, SlackTransport, SlackBridgeConfig

pytestmark = pytest.mark.anyio


def test_slack_presenter_init():
    p = SlackPresenter()
    assert p is not None


class TestSlackTransport:
    async def test_send_success(self):
        bot = MagicMock()

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.ts = "123.456"
        mock_resp.message = MagicMock()
        mock_resp.message.thread_ts = "111.222"
        mock_resp.message.user = "U123"
        bot.send_message = AsyncMock(return_value=mock_resp)

        transport = SlackTransport(bot)
        msg = RenderedMessage(
            text="hello", extra={"followups": [RenderedMessage(text="f1")]}
        )

        ref = await transport.send(
            channel_id="C1",
            message=msg,
            options=SendOptions(thread_id="999"),
        )

        assert ref is not None
        assert ref.channel_id == "C1"
        assert ref.message_id == "123.456"
        assert ref.thread_id == "111.222"
        assert ref.sender_id == "U123"

        bot.send_message.assert_any_call("C1", "hello", thread_ts="999")
        bot.send_message.assert_any_call("C1", "f1", thread_ts="111.222")

    async def test_send_fail(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=None)

        transport = SlackTransport(bot)
        msg = RenderedMessage(text="hello")

        ref = await transport.send(channel_id="C1", message=msg)
        assert ref is None

    async def test_edit_success(self):
        bot = MagicMock()

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.ts = "789.012"
        mock_resp.message = None
        bot.edit_message = AsyncMock(return_value=mock_resp)
        bot.send_message = AsyncMock()

        transport = SlackTransport(bot)
        ref = MessageRef(
            channel_id="C1", message_id="123.456", thread_id="111.222", sender_id="U1"
        )
        msg = RenderedMessage(
            text="new text", extra={"followups": [RenderedMessage(text="f2")]}
        )

        new_ref = await transport.edit(ref=ref, message=msg)
        assert new_ref is not None
        assert new_ref.message_id == "789.012"
        assert new_ref.thread_id == "111.222"

        bot.edit_message.assert_called_once_with("C1", "123.456", "new text", wait=True)
        bot.send_message.assert_called_once_with("C1", "f2", thread_ts="111.222")

    async def test_edit_fail(self):
        bot = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = False
        bot.edit_message = AsyncMock(return_value=mock_resp)

        transport = SlackTransport(bot)
        ref = MessageRef(channel_id="C1", message_id="123.456")
        msg = RenderedMessage(text="new text")

        new_ref = await transport.edit(ref=ref, message=msg)
        assert new_ref is None

    async def test_delete(self):
        bot = MagicMock()
        transport = SlackTransport(bot)
        ref = MessageRef(channel_id="C1", message_id="123")
        res = await transport.delete(ref=ref)
        assert res is True

    async def test_close(self):
        bot = MagicMock()
        bot.close = AsyncMock()
        transport = SlackTransport(bot)
        await transport.close()
        bot.close.assert_called_once()


def test_bridge_config():
    bot = MagicMock()
    runtime = MagicMock()
    exec_cfg = MagicMock()

    cfg = SlackBridgeConfig(
        bot=bot,
        bot_user_id="U1",
        bot_username="bot",
        runtime=runtime,
        channel_id="C1",
        startup_msg="hello",
        exec_cfg=exec_cfg,
    )
    assert cfg.bot == bot
    assert cfg.bot_user_id == "U1"
