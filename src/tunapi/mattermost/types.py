"""Transport-specific types for Mattermost incoming messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class MattermostIncomingMessage:
    transport: str = "mattermost"
    channel_id: str = ""
    post_id: str = ""
    text: str = ""
    root_id: str = ""
    sender_id: str = ""
    sender_username: str = ""
    channel_type: str = ""  # "O", "P", "D", "G"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_direct(self) -> bool:
        return self.channel_type == "D"

    @property
    def is_thread_reply(self) -> bool:
        return bool(self.root_id)


@dataclass(frozen=True, slots=True)
class MattermostReactionEvent:
    """Fired when a reaction is added to a post (used for cancel via 🛑)."""

    transport: str = "mattermost"
    channel_id: str = ""
    post_id: str = ""
    user_id: str = ""
    emoji_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


MattermostIncomingUpdate = MattermostIncomingMessage | MattermostReactionEvent
