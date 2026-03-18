"""Tests for Mattermost WebSocket event parsing."""

from __future__ import annotations

import json

from tunapi.mattermost.api_models import WebSocketBroadcast, WebSocketEvent
from tunapi.mattermost.parsing import parse_posted_event, parse_reaction_event, parse_ws_event
from tunapi.mattermost.types import MattermostIncomingMessage, MattermostReactionEvent


def _posted_event(
    *,
    post_id: str = "p1",
    channel_id: str = "c1",
    user_id: str = "u1",
    message: str = "hello",
    root_id: str = "",
    channel_type: str = "O",
    sender_name: str = "@john",
) -> WebSocketEvent:
    post = {
        "id": post_id,
        "channel_id": channel_id,
        "user_id": user_id,
        "message": message,
        "root_id": root_id,
    }
    return WebSocketEvent(
        event="posted",
        data={
            "post": json.dumps(post),
            "channel_type": channel_type,
            "sender_name": sender_name,
        },
        broadcast=WebSocketBroadcast(channel_id=channel_id),
        seq=1,
    )


class TestParsePostedEvent:
    def test_basic_message(self):
        ev = _posted_event()
        msg = parse_posted_event(ev, bot_user_id="bot1")
        assert isinstance(msg, MattermostIncomingMessage)
        assert msg.post_id == "p1"
        assert msg.text == "hello"
        assert msg.sender_username == "john"
        assert msg.channel_type == "O"

    def test_ignores_bot_own_message(self):
        ev = _posted_event(user_id="bot1")
        msg = parse_posted_event(ev, bot_user_id="bot1")
        assert msg is None

    def test_channel_filter_blocks(self):
        ev = _posted_event(channel_id="c2")
        msg = parse_posted_event(
            ev, bot_user_id="bot1", allowed_channel_ids=["c1"]
        )
        assert msg is None

    def test_channel_filter_allows_dm(self):
        ev = _posted_event(channel_id="c2", channel_type="D")
        msg = parse_posted_event(
            ev, bot_user_id="bot1", allowed_channel_ids=["c1"]
        )
        assert msg is not None

    def test_user_filter_blocks(self):
        ev = _posted_event(user_id="u2")
        msg = parse_posted_event(
            ev, bot_user_id="bot1", allowed_user_ids=["u1"]
        )
        assert msg is None

    def test_user_filter_allows(self):
        ev = _posted_event(user_id="u1")
        msg = parse_posted_event(
            ev, bot_user_id="bot1", allowed_user_ids=["u1"]
        )
        assert msg is not None

    def test_thread_reply(self):
        ev = _posted_event(root_id="p0")
        msg = parse_posted_event(ev, bot_user_id="bot1")
        assert msg is not None
        assert msg.root_id == "p0"
        assert msg.is_thread_reply

    def test_bad_post_json_returns_none(self):
        ev = WebSocketEvent(
            event="posted",
            data={"post": "not-json{{{"},
            broadcast=WebSocketBroadcast(channel_id="c1"),
        )
        msg = parse_posted_event(ev, bot_user_id="bot1")
        assert msg is None

    def test_empty_allowed_channels_allows_all(self):
        ev = _posted_event(channel_id="any")
        msg = parse_posted_event(
            ev, bot_user_id="bot1", allowed_channel_ids=[]
        )
        assert msg is not None


class TestParseReactionEvent:
    def test_cancel_reaction(self):
        ev = WebSocketEvent(
            event="reaction_added",
            data={
                "reaction": json.dumps({
                    "user_id": "u1",
                    "post_id": "p1",
                    "emoji_name": "octagonal_sign",
                    "create_at": 0,
                }),
            },
            broadcast=WebSocketBroadcast(channel_id="c1"),
        )
        result = parse_reaction_event(ev, bot_user_id="bot1")
        assert isinstance(result, MattermostReactionEvent)
        assert result.emoji_name == "octagonal_sign"
        assert result.post_id == "p1"

    def test_ignores_bot_reaction(self):
        ev = WebSocketEvent(
            event="reaction_added",
            data={
                "reaction": json.dumps({
                    "user_id": "bot1",
                    "post_id": "p1",
                    "emoji_name": "octagonal_sign",
                    "create_at": 0,
                }),
            },
            broadcast=WebSocketBroadcast(channel_id="c1"),
        )
        result = parse_reaction_event(ev, bot_user_id="bot1")
        assert result is None


class TestParseWsEvent:
    def test_routes_posted(self):
        ev = _posted_event()
        result = parse_ws_event(ev, bot_user_id="bot1")
        assert isinstance(result, MattermostIncomingMessage)

    def test_routes_reaction(self):
        ev = WebSocketEvent(
            event="reaction_added",
            data={
                "reaction": json.dumps({
                    "user_id": "u1",
                    "post_id": "p1",
                    "emoji_name": "thumbsup",
                    "create_at": 0,
                }),
            },
            broadcast=WebSocketBroadcast(channel_id="c1"),
        )
        result = parse_ws_event(ev, bot_user_id="bot1")
        assert isinstance(result, MattermostReactionEvent)

    def test_ignores_unknown_event(self):
        ev = WebSocketEvent(event="typing", data={}, broadcast=WebSocketBroadcast())
        result = parse_ws_event(ev, bot_user_id="bot1")
        assert result is None
