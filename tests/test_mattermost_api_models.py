"""Tests for Mattermost API data models."""

from __future__ import annotations

import msgspec

from tunapi.mattermost.api_models import (
    Channel,
    FileInfo,
    Post,
    PostList,
    Reaction,
    User,
    WebSocketAuthReply,
    WebSocketEvent,
    decode_post,
    decode_ws_event,
)


class TestUser:
    def test_minimal(self):
        u = User(id="abc123")
        assert u.id == "abc123"
        assert u.username == ""

    def test_from_json(self):
        data = {
            "id": "user1",
            "username": "john",
            "email": "john@example.com",
            "first_name": "John",
            "last_name": "Doe",
            "roles": "system_user",
            "extra_field": True,  # should be tolerated
        }
        u = msgspec.convert(data, User)
        assert u.id == "user1"
        assert u.username == "john"
        assert u.roles == "system_user"


class TestChannel:
    def test_direct_message(self):
        data = {"id": "ch1", "type": "D", "display_name": ""}
        ch = msgspec.convert(data, Channel)
        assert ch.type == "D"

    def test_open_channel(self):
        data = {
            "id": "ch2",
            "team_id": "team1",
            "type": "O",
            "display_name": "General",
            "name": "general",
        }
        ch = msgspec.convert(data, Channel)
        assert ch.display_name == "General"


class TestPost:
    def test_simple_post(self):
        data = {
            "id": "post1",
            "channel_id": "ch1",
            "user_id": "user1",
            "message": "hello",
            "create_at": 1700000000000,
        }
        p = msgspec.convert(data, Post)
        assert p.id == "post1"
        assert p.message == "hello"
        assert p.root_id == ""
        assert p.props == {}
        assert p.file_ids == []

    def test_reply_post(self):
        data = {
            "id": "post2",
            "channel_id": "ch1",
            "user_id": "user1",
            "root_id": "post1",
            "message": "reply",
        }
        p = msgspec.convert(data, Post)
        assert p.root_id == "post1"

    def test_with_props(self):
        data = {
            "id": "post3",
            "channel_id": "ch1",
            "message": "with props",
            "props": {"attachments": [{"text": "attached"}]},
        }
        p = msgspec.convert(data, Post)
        assert "attachments" in p.props

    def test_decode_post(self):
        raw = b'{"id":"p1","channel_id":"c1","message":"hi"}'
        p = decode_post(raw)
        assert p.id == "p1"
        assert p.message == "hi"


class TestPostList:
    def test_post_list(self):
        data = {
            "order": ["post1", "post2"],
            "posts": {
                "post1": {"id": "post1", "message": "first"},
                "post2": {"id": "post2", "message": "second"},
            },
        }
        pl = msgspec.convert(data, PostList)
        assert len(pl.order) == 2
        assert pl.posts["post1"].message == "first"


class TestFileInfo:
    def test_file_info(self):
        data = {
            "id": "file1",
            "name": "test.py",
            "size": 1024,
            "mime_type": "text/x-python",
            "extension": "py",
        }
        fi = msgspec.convert(data, FileInfo)
        assert fi.name == "test.py"
        assert fi.size == 1024


class TestReaction:
    def test_reaction(self):
        r = Reaction(user_id="u1", post_id="p1", emoji_name="thumbsup")
        assert r.emoji_name == "thumbsup"


class TestWebSocketEvent:
    def test_posted_event(self):
        data = {
            "event": "posted",
            "data": {
                "channel_display_name": "General",
                "post": '{"id":"p1","channel_id":"c1","message":"hello"}',
                "sender_name": "@john",
            },
            "broadcast": {
                "channel_id": "c1",
                "team_id": "t1",
                "user_id": "",
            },
            "seq": 5,
        }
        ev = msgspec.convert(data, WebSocketEvent)
        assert ev.event == "posted"
        assert ev.seq == 5
        assert ev.broadcast.channel_id == "c1"
        # data["post"] is a JSON string — caller must decode separately
        assert isinstance(ev.data["post"], str)

    def test_reaction_added_event(self):
        data = {
            "event": "reaction_added",
            "data": {
                "reaction": '{"user_id":"u1","post_id":"p1","emoji_name":"octagonal_sign"}',
            },
            "broadcast": {"channel_id": "c1"},
            "seq": 10,
        }
        ev = msgspec.convert(data, WebSocketEvent)
        assert ev.event == "reaction_added"


class TestDecodeWsEvent:
    def test_decode_regular_event(self):
        raw = b'{"event":"posted","data":{},"broadcast":{"channel_id":"c1"},"seq":1}'
        result = decode_ws_event(raw)
        assert isinstance(result, WebSocketEvent)
        assert result.event == "posted"

    def test_decode_auth_reply(self):
        raw = b'{"seq_reply":1,"status":"OK"}'
        result = decode_ws_event(raw)
        assert isinstance(result, WebSocketAuthReply)
        assert result.status == "OK"

    def test_decode_string_input(self):
        raw = '{"event":"typing","data":{},"broadcast":{},"seq":2}'
        result = decode_ws_event(raw)
        assert isinstance(result, WebSocketEvent)
        assert result.event == "typing"
