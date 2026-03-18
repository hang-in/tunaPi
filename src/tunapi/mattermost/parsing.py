"""Parse Mattermost WebSocket events into typed incoming messages."""

from __future__ import annotations

import json
from collections.abc import Iterable

from ..logging import get_logger
from .api_models import Post, Reaction, WebSocketEvent, decode_post
from .types import MattermostIncomingMessage, MattermostIncomingUpdate, MattermostReactionEvent

logger = get_logger(__name__)


def _decode_nested_json(data: dict[str, object], key: str) -> dict | None:
    """Decode a JSON-encoded string nested inside a WebSocket data dict."""
    raw = data.get(key)
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def parse_posted_event(
    event: WebSocketEvent,
    *,
    bot_user_id: str,
    allowed_channel_ids: Iterable[str] | None = None,
    allowed_user_ids: Iterable[str] | None = None,
) -> MattermostIncomingMessage | None:
    """Parse a ``posted`` WebSocket event into a typed message.

    Returns ``None`` if the message should be ignored (from the bot itself,
    from a disallowed channel/user, or unparseable).
    """
    post_json = _decode_nested_json(event.data, "post")
    if post_json is None:
        logger.debug("mattermost.parse_skip", reason="no_post_json")
        return None

    try:
        post = decode_post(json.dumps(post_json).encode())
    except Exception as exc:  # noqa: BLE001
        logger.warning("mattermost.parse_error", error=str(exc))
        return None

    # Ignore bot's own posts
    if post.user_id == bot_user_id:
        return None

    # Channel filter
    if allowed_channel_ids is not None:
        ids = set(allowed_channel_ids)
        if ids and post.channel_id not in ids:
            channel_type = str(event.data.get("channel_type", ""))
            # Always allow DMs even if not in whitelist
            if channel_type != "D":
                return None

    # User filter
    if allowed_user_ids is not None:
        ids = set(allowed_user_ids)
        if ids and post.user_id not in ids:
            return None

    channel_type = str(event.data.get("channel_type", ""))
    sender_name = str(event.data.get("sender_name", ""))

    return MattermostIncomingMessage(
        channel_id=post.channel_id,
        post_id=post.id,
        text=post.message.strip(),
        root_id=post.root_id,
        sender_id=post.user_id,
        sender_username=sender_name.lstrip("@"),
        channel_type=channel_type,
        raw=post_json,
    )


def parse_reaction_event(
    event: WebSocketEvent,
    *,
    bot_user_id: str,
) -> MattermostReactionEvent | None:
    """Parse a ``reaction_added`` event.  Used for cancel-by-reaction."""
    reaction_json = _decode_nested_json(event.data, "reaction")
    if reaction_json is None:
        return None

    try:
        import msgspec

        reaction = msgspec.convert(reaction_json, Reaction)
    except Exception:  # noqa: BLE001
        return None

    # Ignore bot's own reactions
    if reaction.user_id == bot_user_id:
        return None

    return MattermostReactionEvent(
        channel_id=event.broadcast.channel_id,
        post_id=reaction.post_id,
        user_id=reaction.user_id,
        emoji_name=reaction.emoji_name,
        raw=reaction_json,
    )


def parse_ws_event(
    event: WebSocketEvent,
    *,
    bot_user_id: str,
    allowed_channel_ids: Iterable[str] | None = None,
    allowed_user_ids: Iterable[str] | None = None,
) -> MattermostIncomingUpdate | None:
    """Route a WebSocket event to the appropriate parser."""
    if event.event == "posted":
        return parse_posted_event(
            event,
            bot_user_id=bot_user_id,
            allowed_channel_ids=allowed_channel_ids,
            allowed_user_ids=allowed_user_ids,
        )
    if event.event == "reaction_added":
        return parse_reaction_event(event, bot_user_id=bot_user_id)
    return None
