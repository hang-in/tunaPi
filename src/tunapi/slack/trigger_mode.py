"""Trigger mode logic for Slack transport."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.trigger import resolve_trigger_mode as _core_resolve
from .parsing import SlackMessageEvent

if TYPE_CHECKING:
    from .chat_prefs import ChatPrefsStore


async def resolve_trigger_mode(
    channel_id: str,
    chat_prefs: ChatPrefsStore | None,
) -> str:
    """Resolve the effective trigger mode for a channel."""
    return await _core_resolve(channel_id, chat_prefs, default="mentions")


def should_trigger(
    event: SlackMessageEvent,
    *,
    bot_user_id: str,
    trigger_mode: str,
) -> bool:
    """Check if the bot should respond to an incoming message."""
    # Always trigger on direct messages (IM)
    if event.channel_id.startswith("D"):
        return True

    if trigger_mode == "all":
        return True

    # Always trigger on thread replies (bot already participating)
    if event.thread_ts:
        return True

    # Otherwise, only trigger if the bot is mentioned
    return f"<@{bot_user_id}>" in event.text


def strip_mention(text: str, bot_user_id: str) -> str:
    """Remove the bot's mention from the message text."""
    mention = f"<@{bot_user_id}>"
    return text.replace(mention, "").strip()
