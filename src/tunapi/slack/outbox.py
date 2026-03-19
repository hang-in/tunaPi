"""Re-export shared Outbox for Slack transport."""

from ..core.outbox import (
    DELETE_PRIORITY,
    EDIT_PRIORITY,
    SEND_PRIORITY,
    Outbox as SlackOutbox,
    OutboxOp,
)

__all__ = [
    "DELETE_PRIORITY",
    "EDIT_PRIORITY",
    "OutboxOp",
    "SEND_PRIORITY",
    "SlackOutbox",
]
