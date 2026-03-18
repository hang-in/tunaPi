"""Msgspec models for Mattermost API v4 payloads (subset used by tunapi)."""

from __future__ import annotations

import msgspec

__all__ = [
    "Channel",
    "FileInfo",
    "Post",
    "PostList",
    "Reaction",
    "User",
    "WebSocketEvent",
    "decode_post",
    "decode_ws_event",
]


class User(msgspec.Struct, forbid_unknown_fields=False):
    id: str
    username: str = ""
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    nickname: str = ""
    roles: str = ""


class Channel(msgspec.Struct, forbid_unknown_fields=False):
    id: str
    team_id: str = ""
    type: str = ""  # "O" open, "P" private, "D" direct, "G" group
    display_name: str = ""
    name: str = ""
    header: str = ""
    purpose: str = ""


class Post(msgspec.Struct, forbid_unknown_fields=False):
    id: str
    channel_id: str = ""
    user_id: str = ""
    root_id: str = ""
    message: str = ""
    type: str = ""
    create_at: int = 0
    update_at: int = 0
    delete_at: int = 0
    props: dict[str, object] = msgspec.field(default_factory=dict)
    file_ids: list[str] = msgspec.field(default_factory=list)


class PostList(msgspec.Struct, forbid_unknown_fields=False):
    order: list[str] = msgspec.field(default_factory=list)
    posts: dict[str, Post] = msgspec.field(default_factory=dict)


class FileInfo(msgspec.Struct, forbid_unknown_fields=False):
    id: str
    name: str = ""
    size: int = 0
    mime_type: str = ""
    extension: str = ""


class Reaction(msgspec.Struct, forbid_unknown_fields=False):
    user_id: str
    post_id: str
    emoji_name: str
    create_at: int = 0


class WebSocketBroadcast(msgspec.Struct, forbid_unknown_fields=False):
    channel_id: str = ""
    team_id: str = ""
    user_id: str = ""


class WebSocketEvent(msgspec.Struct, forbid_unknown_fields=False):
    """Mattermost WebSocket event.

    The ``data`` dict may contain JSON-encoded strings (e.g. ``data["post"]``
    is a JSON string that decodes to a :class:`Post`).
    """

    event: str = ""
    data: dict[str, object] = msgspec.field(default_factory=dict)
    broadcast: WebSocketBroadcast = msgspec.field(
        default_factory=WebSocketBroadcast,
    )
    seq: int = 0


class WebSocketAuthReply(msgspec.Struct, forbid_unknown_fields=False):
    """Reply to WebSocket authentication."""

    seq_reply: int = 0
    status: str = ""


_POST_DECODER = msgspec.json.Decoder(Post)
_WS_EVENT_DECODER = msgspec.json.Decoder(WebSocketEvent)
_WS_AUTH_REPLY_DECODER = msgspec.json.Decoder(WebSocketAuthReply)


def decode_post(payload: str | bytes) -> Post:
    return _POST_DECODER.decode(payload)


def decode_ws_event(payload: str | bytes) -> WebSocketEvent | WebSocketAuthReply:
    """Decode a WebSocket frame.

    Auth replies have ``seq_reply``; regular events have ``event``.
    """
    raw = payload if isinstance(payload, bytes) else payload.encode()
    # Peek: auth replies contain "seq_reply", events contain "event"
    try:
        obj = _WS_EVENT_DECODER.decode(raw)
    except msgspec.DecodeError:
        return _WS_AUTH_REPLY_DECODER.decode(raw)
    if not obj.event:
        return _WS_AUTH_REPLY_DECODER.decode(raw)
    return obj
