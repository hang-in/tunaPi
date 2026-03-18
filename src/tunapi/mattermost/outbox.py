"""Priority outbox queue with rate limiting for Mattermost API calls."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Hashable

import anyio

from ..logging import get_logger
from .client_api import MattermostRetryAfter

logger = get_logger(__name__)

SEND_PRIORITY = 0
DELETE_PRIORITY = 1
EDIT_PRIORITY = 2


@dataclass(slots=True)
class OutboxOp:
    execute: Callable[[], Awaitable[Any]]
    priority: int
    queued_at: float
    label: str | None = None
    done: anyio.Event = field(default_factory=anyio.Event)
    result: Any = None

    def set_result(self, result: Any) -> None:
        self.result = result
        self.done.set()


class MattermostOutbox:
    """Serialised, priority-based outbox for Mattermost API calls.

    Mirrors the Telegram outbox pattern: operations are enqueued with a key
    (for deduplication) and a priority.  A single background worker drains
    the queue respecting rate-limit intervals and server retry-after headers.
    """

    def __init__(
        self,
        *,
        interval: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
        on_error: Callable[[str, Exception], None] | None = None,
        on_outbox_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._on_error = on_error
        self._on_outbox_error = on_outbox_error

        self._pending: dict[Hashable, OutboxOp] = {}
        self._cond = anyio.Condition()
        self._start_lock = anyio.Lock()
        self._closed = False
        self._tg: anyio.abc.TaskGroup | None = None
        self.next_at: float = 0.0
        self.retry_at: float = 0.0

    async def _ensure_worker(self) -> None:
        async with self._start_lock:
            if self._tg is not None:
                return
            tg = anyio.create_task_group()
            self._tg = tg
            await tg.__aenter__()
            tg.start_soon(self._run)

    async def enqueue(
        self,
        key: Hashable,
        op: OutboxOp,
        *,
        wait: bool = True,
    ) -> Any:
        await self._ensure_worker()
        async with self._cond:
            prev = self._pending.get(key)
            if prev is not None:
                op.queued_at = prev.queued_at
                prev.set_result(None)
            self._pending[key] = op
            self._cond.notify()

        if wait:
            await op.done.wait()
            return op.result
        return None

    async def drop_pending(self, key: Hashable) -> None:
        async with self._cond:
            op = self._pending.pop(key, None)
            if op is not None:
                op.set_result(None)
            self._cond.notify()

    def _pick_locked(self) -> tuple[Hashable, OutboxOp] | None:
        if not self._pending:
            return None
        return min(
            self._pending.items(),
            key=lambda item: (item[1].priority, item[1].queued_at),
        )

    async def _execute_op(self, op: OutboxOp) -> Any:
        try:
            return await op.execute()
        except MattermostRetryAfter:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._on_error:
                self._on_error(op.label or "unknown", exc)
            return None

    async def _run(self) -> None:
        try:
            while not self._closed:
                async with self._cond:
                    while not self._pending and not self._closed:
                        await self._cond.wait()
                    if self._closed:
                        break

                blocked_until = max(self.next_at, self.retry_at)
                now = self._clock()
                if now < blocked_until:
                    await self._sleep(blocked_until - now)

                async with self._cond:
                    picked = self._pick_locked()
                    if picked is None:
                        continue
                    key, op = picked
                    del self._pending[key]

                started_at = self._clock()
                try:
                    result = await self._execute_op(op)
                except MattermostRetryAfter as exc:
                    self.retry_at = max(
                        self.retry_at, self._clock() + exc.retry_after
                    )
                    async with self._cond:
                        self._pending[key] = op
                        self._cond.notify()
                    continue

                self.next_at = started_at + self._interval
                op.set_result(result)
        except Exception as exc:  # noqa: BLE001
            self._closed = True
            async with self._cond:
                for op in self._pending.values():
                    op.set_result(None)
                self._pending.clear()
            if self._on_outbox_error:
                self._on_outbox_error(exc)

    async def close(self) -> None:
        self._closed = True
        async with self._cond:
            self._cond.notify()
