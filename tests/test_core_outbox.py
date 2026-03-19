"""Direct unit tests for core/outbox.py."""

from __future__ import annotations

import anyio

from tunapi.core.outbox import (
    EDIT_PRIORITY,
    SEND_PRIORITY,
    Outbox,
    OutboxOp,
    RetryAfter,
)


class TestOutboxBasic:
    def test_enqueue_and_execute(self):
        """Enqueue an op and get the result."""
        results: list[str] = []

        async def _run():
            outbox = Outbox(interval=0)
            try:
                op = OutboxOp(
                    execute=lambda: _async_return("hello"),
                    priority=SEND_PRIORITY,
                    queued_at=0.0,
                    label="test",
                )
                result = await outbox.enqueue("key1", op)
                results.append(result)
            finally:
                await outbox.close()

        anyio.run(_run)
        assert results == ["hello"]

    def test_deduplication(self):
        """Later enqueue with same key replaces previous (both resolve)."""
        results: list[str | None] = []

        gate = anyio.Event()

        async def _blocked():
            await gate.wait()
            return "should_not_run"

        async def _run():
            # Use long interval to block worker, ensuring dedup happens
            outbox = Outbox(interval=0)
            try:
                # First op blocks until gate is set
                op1 = OutboxOp(
                    execute=_blocked,
                    priority=SEND_PRIORITY,
                    queued_at=0.0,
                )
                op2 = OutboxOp(
                    execute=lambda: _async_return("second"),
                    priority=SEND_PRIORITY,
                    queued_at=1.0,
                )
                await outbox.enqueue("dup", op1, wait=False)
                # op1 is being processed; enqueue op2 with same key
                # op1 will be replaced and resolved with None
                await anyio.sleep(0)
                await outbox.enqueue("dup", op2, wait=False)
                gate.set()
                await op2.done.wait()
                results.append(op2.result)
            finally:
                await outbox.close()

        anyio.run(_run)
        assert results[0] == "second"


class TestRetryAfter:
    def test_retry_after_requeues(self):
        """RetryAfter exception causes re-enqueue."""
        call_count = 0

        class MyRetry(RetryAfter):
            pass

        async def _flaky():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise MyRetry(0.01)
            return "ok"

        async def _run():
            outbox = Outbox(interval=0, retry_after_type=MyRetry)
            try:
                op = OutboxOp(
                    execute=_flaky,
                    priority=SEND_PRIORITY,
                    queued_at=0.0,
                )
                result = await outbox.enqueue("k", op)
                assert result == "ok"
            finally:
                await outbox.close()

        anyio.run(_run)
        assert call_count == 2

    def test_non_retry_error_returns_none(self):
        """Non-retry exceptions return None (via on_error callback)."""
        errors: list[str] = []

        async def _fail():
            raise ValueError("boom")

        async def _run():
            outbox = Outbox(
                interval=0,
                on_error=lambda label, exc: errors.append(str(exc)),
            )
            try:
                op = OutboxOp(
                    execute=_fail,
                    priority=SEND_PRIORITY,
                    queued_at=0.0,
                    label="test_op",
                )
                result = await outbox.enqueue("k", op)
                assert result is None
            finally:
                await outbox.close()

        anyio.run(_run)
        assert errors == ["boom"]


class TestOutboxClose:
    def test_close_resolves_pending(self):
        """Close resolves pending ops with None."""

        async def _run():
            outbox = Outbox(interval=10)  # long interval so op stays pending
            op = OutboxOp(
                execute=lambda: _async_return("x"),
                priority=SEND_PRIORITY,
                queued_at=0.0,
            )
            await outbox.enqueue("k", op, wait=False)
            await outbox.close()
            # After close, pending ops should be resolved
            assert op.done.is_set()

        anyio.run(_run)

    def test_drop_pending(self):
        """drop_pending removes a specific key."""

        async def _run():
            outbox = Outbox(interval=10)
            op = OutboxOp(
                execute=lambda: _async_return("x"),
                priority=SEND_PRIORITY,
                queued_at=0.0,
            )
            await outbox.enqueue("k", op, wait=False)
            await outbox.drop_pending("k")
            assert op.done.is_set()
            assert op.result is None
            await outbox.close()

        anyio.run(_run)


async def _async_return(value):
    return value
