"""Discord outbox for rate-limited message operations."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Hashable

    from anyio.abc import TaskGroup

__all__ = [
    "DELETE_PRIORITY",
    "EDIT_PRIORITY",
    "DiscordOutbox",
    "OutboxOp",
    "RetryAfter",
    "SEND_PRIORITY",
]

# Priority constants (lower = higher priority)
SEND_PRIORITY = 0
DELETE_PRIORITY = 1
EDIT_PRIORITY = 2

# Default rate limit interval: 5 messages per 5 seconds = 1 msg/sec
# Using 0.2s allows burst of ~5 msg/sec with safety margin
DEFAULT_CHANNEL_INTERVAL = 0.2


class RetryAfter(Exception):
    """Raised when Discord returns a rate limit response."""

    def __init__(self, retry_after: float, description: str | None = None) -> None:
        super().__init__(description or f"retry after {retry_after}")
        self.retry_after = float(retry_after)
        self.description = description


@dataclass(slots=True)
class OutboxOp:
    """A queued operation in the outbox."""

    execute: Callable[[], Awaitable[Any]]
    priority: int
    queued_at: float
    channel_id: int | None
    label: str | None = None
    done: anyio.Event = field(default_factory=anyio.Event)
    result: Any = None

    def set_result(self, result: Any) -> None:
        """Set the result and mark as done."""
        if self.done.is_set():
            return
        self.result = result
        self.done.set()


class DiscordOutbox:
    """Rate-limited outbox for Discord message operations.

    Queues send/edit/delete operations and applies per-channel rate limiting.
    Operations are processed in priority order (send > delete > edit).
    Edit operations to the same message are coalesced (only last one is sent).
    """

    def __init__(
        self,
        *,
        interval_for_channel: Callable[[int | None], float] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
        on_error: Callable[[OutboxOp, Exception], None] | None = None,
        on_outbox_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Initialize the outbox.

        Args:
            interval_for_channel: Callback to get rate limit interval for a channel.
                                  If None, uses DEFAULT_CHANNEL_INTERVAL.
            clock: Clock function for timing (defaults to time.monotonic).
            sleep: Sleep function (defaults to anyio.sleep).
            on_error: Callback for individual operation errors.
            on_outbox_error: Callback for fatal outbox errors.
        """
        self._interval_for_channel = interval_for_channel or (
            lambda _: DEFAULT_CHANNEL_INTERVAL
        )
        self._clock = clock
        self._sleep = sleep
        self._on_error = on_error
        self._on_outbox_error = on_outbox_error
        self._pending: dict[Hashable, OutboxOp] = {}
        self._cond = anyio.Condition()
        self._start_lock = anyio.Lock()
        self._closed = False
        self._tg: TaskGroup | None = None
        self.next_at = 0.0
        self.retry_at = 0.0

    async def ensure_worker(self) -> None:
        """Ensure the worker task is running."""
        async with self._start_lock:
            if self._tg is not None or self._closed:
                return
            self._tg = await anyio.create_task_group().__aenter__()
            self._tg.start_soon(self.run)

    async def enqueue(self, *, key: Hashable, op: OutboxOp, wait: bool = True) -> Any:
        """Enqueue an operation.

        If an operation with the same key already exists, it will be replaced
        (coalesced) and the previous operation will return None.

        Args:
            key: Unique key for the operation. Same key = coalesce.
            op: The operation to enqueue.
            wait: If True, wait for the operation to complete.

        Returns:
            The result of the operation, or None if not waiting or coalesced.
        """
        await self.ensure_worker()
        async with self._cond:
            if self._closed:
                op.set_result(None)
                return op.result
            previous = self._pending.get(key)
            if previous is not None:
                # Preserve original queue time for priority ordering
                op.queued_at = previous.queued_at
                previous.set_result(None)
            self._pending[key] = op
            self._cond.notify()
        if not wait:
            return None
        await op.done.wait()
        return op.result

    async def drop_pending(self, *, key: Hashable) -> None:
        """Drop a pending operation.

        Used to cancel pending edits when deleting a message.
        """
        async with self._cond:
            pending = self._pending.pop(key, None)
            if pending is not None:
                pending.set_result(None)
            self._cond.notify()

    async def close(self) -> None:
        """Close the outbox and cancel all pending operations."""
        async with self._cond:
            self._closed = True
            self._fail_pending()
            self._cond.notify_all()
        if self._tg is not None:
            await self._tg.__aexit__(None, None, None)
            self._tg = None

    def _fail_pending(self) -> None:
        """Fail all pending operations."""
        for pending in list(self._pending.values()):
            pending.set_result(None)
        self._pending.clear()

    def _pick_locked(self) -> tuple[Hashable, OutboxOp] | None:
        """Pick the next operation to execute (highest priority, oldest)."""
        if not self._pending:
            return None
        return min(
            self._pending.items(),
            key=lambda item: (item[1].priority, item[1].queued_at),
        )

    async def _execute_op(self, op: OutboxOp) -> Any:
        """Execute an operation with error handling."""
        try:
            return await op.execute()
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, RetryAfter):
                raise
            if self._on_error is not None:
                self._on_error(op, exc)
            return None

    async def _sleep_until(self, deadline: float) -> None:
        """Sleep until the given deadline."""
        delay = deadline - self._clock()
        if delay > 0:
            await self._sleep(delay)

    async def run(self) -> None:
        """Main worker loop."""
        cancel_exc = anyio.get_cancelled_exc_class()
        try:
            while True:
                # Wait for work
                async with self._cond:
                    while not self._pending and not self._closed:
                        await self._cond.wait()
                    if self._closed and not self._pending:
                        return

                # Check rate limit timing
                blocked_until = max(self.next_at, self.retry_at)
                if self._clock() < blocked_until:
                    await self._sleep_until(blocked_until)
                    continue

                # Pick and execute next operation
                async with self._cond:
                    if self._closed and not self._pending:
                        return
                    picked = self._pick_locked()
                    if picked is None:
                        continue
                    key, op = picked
                    self._pending.pop(key, None)

                started_at = self._clock()
                try:
                    result = await self._execute_op(op)
                except RetryAfter as exc:
                    # Re-schedule after rate limit delay
                    self.retry_at = max(self.retry_at, self._clock() + exc.retry_after)
                    async with self._cond:
                        if self._closed:
                            op.set_result(None)
                        elif key not in self._pending:
                            # Re-enqueue if not replaced by another op
                            self._pending[key] = op
                            self._cond.notify()
                        else:
                            # Another op replaced this one, discard
                            op.set_result(None)
                    continue

                # Update rate limit timing
                self.next_at = started_at + self._interval_for_channel(op.channel_id)
                op.set_result(result)

        except cancel_exc:
            return
        except Exception as exc:  # noqa: BLE001
            async with self._cond:
                self._closed = True
                self._fail_pending()
                self._cond.notify_all()
            if self._on_outbox_error is not None:
                self._on_outbox_error(exc)
            return
