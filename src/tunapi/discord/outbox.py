"""Discord outbox wrapper using core Outbox."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import anyio

from tunapi.core.outbox import (
    DELETE_PRIORITY,
    EDIT_PRIORITY,
    SEND_PRIORITY,
    Outbox,
    OutboxOp as CoreOutboxOp,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Hashable

# Re-export priorities for compatibility
__all__ = [
    "DELETE_PRIORITY",
    "EDIT_PRIORITY",
    "DiscordOutbox",
    "OutboxOp",
    "RetryAfter",
    "SEND_PRIORITY",
]

# Keep DEFAULT_CHANNEL_INTERVAL for compatibility
DEFAULT_CHANNEL_INTERVAL = 0.2


class RetryAfter(Exception):
    """Raised when Discord returns a rate limit response."""

    def __init__(self, retry_after: float, description: str | None = None) -> None:
        super().__init__(description or f"retry after {retry_after}")
        self.retry_after = float(retry_after)
        self.description = description


class OutboxOp(CoreOutboxOp):
    """A compatibility wrapper around CoreOutboxOp for Discord."""

    def __init__(
        self,
        execute: Callable[[], Awaitable[Any]],
        priority: int,
        queued_at: float,
        channel_id: int | None,
        label: str | None = None,
    ) -> None:
        super().__init__(
            execute=execute,
            priority=priority,
            queued_at=queued_at,
            chat_id=channel_id,
            label=label,
        )

    @property
    def channel_id(self) -> int | None:
        return self.chat_id


class DiscordOutbox(Outbox):
    """Rate-limited outbox shim for Discord message operations."""

    def __init__(
        self,
        *,
        interval_for_channel: Callable[[int | None], float] | None = None,
        clock: Any = None,
        sleep: Any = None,
        on_error: Callable[[OutboxOp, Exception], None] | None = None,
        on_outbox_error: Callable[[Exception], None] | None = None,
    ) -> None:
        # Wrap on_error to match core on_error_op signature
        def wrapped_on_error(op: CoreOutboxOp, exc: Exception) -> None:
            if on_error is not None:
                # CoreOutboxOp can be duck-typed as OutboxOp
                on_error(op, exc)

        interval_val = (
            (lambda op: interval_for_channel(op.chat_id))
            if interval_for_channel is not None
            else DEFAULT_CHANNEL_INTERVAL
        )

        super().__init__(
            interval=interval_val,
            retry_after_type=RetryAfter,
            clock=clock or time.monotonic,
            sleep=sleep or anyio.sleep,
            on_error_op=wrapped_on_error,
            on_outbox_error=on_outbox_error,
        )

    async def ensure_worker(self) -> None:
        """Compatibility wrapper for the original DiscordOutbox API."""
        if self._closed:
            return
        await self._ensure_worker()

    async def _sleep_until(self, deadline: float) -> None:
        delay = deadline - self._clock()
        if delay > 0:
            await self._sleep(delay)

    async def enqueue(self, *, key: Hashable, op: OutboxOp, wait: bool = True) -> Any:
        if self._closed:
            op.set_result(None)
            return op.result if wait else None
        return await super().enqueue(key, op, wait=wait)

    async def drop_pending(self, *, key: Hashable) -> None:
        await super().drop_pending(key)

    async def close(self) -> None:
        self._closed = True
        async with self._cond:
            for op in self._pending.values():
                op.set_result(None)
            self._pending.clear()
            self._cond.notify()

        if self._tg is not None:
            await self._tg.__aexit__(None, None, None)
            self._tg = None
