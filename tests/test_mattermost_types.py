"""Tests for Mattermost transport types."""

from tunapi.mattermost.types import MattermostIncomingMessage, MattermostReactionEvent


class TestMattermostIncomingMessage:
    def test_defaults(self):
        msg = MattermostIncomingMessage()
        assert msg.transport == "mattermost"
        assert msg.channel_id == ""
        assert not msg.is_direct
        assert not msg.is_thread_reply

    def test_direct_message(self):
        msg = MattermostIncomingMessage(channel_type="D")
        assert msg.is_direct

    def test_thread_reply(self):
        msg = MattermostIncomingMessage(root_id="p0")
        assert msg.is_thread_reply

    def test_not_thread_reply(self):
        msg = MattermostIncomingMessage(root_id="")
        assert not msg.is_thread_reply


class TestMattermostReactionEvent:
    def test_defaults(self):
        ev = MattermostReactionEvent()
        assert ev.transport == "mattermost"
        assert ev.emoji_name == ""

    def test_cancel_emoji(self):
        ev = MattermostReactionEvent(emoji_name="octagonal_sign")
        assert ev.emoji_name == "octagonal_sign"
