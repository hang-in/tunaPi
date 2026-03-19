"""Re-export shared Outbox for Mattermost transport."""

from ..core.outbox import (
    DELETE_PRIORITY,
    EDIT_PRIORITY,
    SEND_PRIORITY,
    Outbox as MattermostOutbox,
    OutboxOp,
)

__all__ = [
    "DELETE_PRIORITY",
    "EDIT_PRIORITY",
    "MattermostOutbox",
    "OutboxOp",
    "SEND_PRIORITY",
]
