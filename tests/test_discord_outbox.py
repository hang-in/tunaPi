"""Comprehensive tests for discord/outbox.py."""

from __future__ import annotations

import pytest
import anyio

from tunapi.discord.outbox import (
    DEFAULT_CHANNEL_INTERVAL,
    DELETE_PRIORITY,
    EDIT_PRIORITY,
    SEND_PRIORITY,
    DiscordOutbox,
    OutboxOp,
    RetryAfter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _async_return(value):
    return value


def _make_op(
    result_value="ok",
    priority=SEND_PRIORITY,
    queued_at=0.0,
    channel_id=None,
    label=None,
    execute=None,
):
    return OutboxOp(
        execute=execute or (lambda: _async_return(result_value)),
        priority=priority,
        queued_at=queued_at,
        channel_id=channel_id,
        label=label,
    )


class FakeClock:
    """Controllable clock for deterministic timing tests."""

    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeSleep:
    """Records sleep calls and advances the clock."""

    def __init__(self, clock: FakeClock):
        self._clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self._clock.advance(seconds)


# ---------------------------------------------------------------------------
# Priority constants
# ---------------------------------------------------------------------------


class TestPriorityConstants:
    def test_send_is_highest_priority(self):
        assert SEND_PRIORITY < DELETE_PRIORITY < EDIT_PRIORITY

    def test_default_channel_interval(self):
        assert DEFAULT_CHANNEL_INTERVAL == 0.2


# ---------------------------------------------------------------------------
# RetryAfter
# ---------------------------------------------------------------------------


class TestRetryAfter:
    def test_retry_after_attributes(self):
        exc = RetryAfter(1.5, "rate limited")
        assert exc.retry_after == 1.5
        assert exc.description == "rate limited"
        assert "rate limited" in str(exc)

    def test_retry_after_default_description(self):
        exc = RetryAfter(2.0)
        assert exc.description is None
        assert "retry after 2.0" in str(exc)

    def test_retry_after_coerces_to_float(self):
        exc = RetryAfter(3)
        assert isinstance(exc.retry_after, float)
        assert exc.retry_after == 3.0


# ---------------------------------------------------------------------------
# OutboxOp
# ---------------------------------------------------------------------------


class TestOutboxOp:
    def test_set_result(self):
        op = _make_op()
        assert not op.done.is_set()
        op.set_result("hello")
        assert op.done.is_set()
        assert op.result == "hello"

    def test_set_result_idempotent(self):
        """Second set_result is ignored."""
        op = _make_op()
        op.set_result("first")
        op.set_result("second")
        assert op.result == "first"

    def test_default_result_is_none(self):
        op = _make_op()
        assert op.result is None


# ---------------------------------------------------------------------------
# DiscordOutbox — basic enqueue/execute
# ---------------------------------------------------------------------------


class TestDiscordOutboxBasic:
    @pytest.mark.anyio
    async def test_enqueue_and_execute(self):
        outbox = DiscordOutbox(interval_for_channel=lambda _: 0)
        try:
            op = _make_op(result_value="hello")
            result = await outbox.enqueue(key="k1", op=op)
            assert result == "hello"
        finally:
            await outbox.close()

    @pytest.mark.anyio
    async def test_enqueue_no_wait(self):
        outbox = DiscordOutbox(interval_for_channel=lambda _: 0)
        try:
            op = _make_op(result_value="data")
            result = await outbox.enqueue(key="k1", op=op, wait=False)
            assert result is None
            # Op should still eventually complete
            await op.done.wait()
            assert op.result == "data"
        finally:
            await outbox.close()

    @pytest.mark.anyio
    async def test_multiple_ops_different_keys(self):
        outbox = DiscordOutbox(interval_for_channel=lambda _: 0)
        try:
            results = []
            for i in range(3):
                op = _make_op(result_value=f"val{i}", queued_at=float(i))
                r = await outbox.enqueue(key=f"key{i}", op=op)
                results.append(r)
            assert results == ["val0", "val1", "val2"]
        finally:
            await outbox.close()


# ---------------------------------------------------------------------------
# Coalescing (deduplication)
# ---------------------------------------------------------------------------


class TestCoalescing:
    @pytest.mark.anyio
    async def test_same_key_coalesces(self):
        """Later enqueue with same key replaces the pending op."""
        gate = anyio.Event()

        async def _blocked():
            await gate.wait()
            return "blocked_result"

        outbox = DiscordOutbox(interval_for_channel=lambda _: 0)
        try:
            # First op blocks the worker
            blocking_op = _make_op(execute=_blocked)
            await outbox.enqueue(key="blocker", op=blocking_op, wait=False)

            # While worker is busy with blocker, enqueue two ops with same key
            await anyio.sleep(0)  # yield to let worker pick up blocker
            op1 = _make_op(result_value="first", queued_at=1.0)
            op2 = _make_op(result_value="second", queued_at=2.0)

            await outbox.enqueue(key="dup", op=op1, wait=False)
            await outbox.enqueue(key="dup", op=op2, wait=False)

            # op1 should be resolved with None (coalesced)
            await op1.done.wait()
            assert op1.result is None

            # Release blocker and let op2 execute
            gate.set()
            await op2.done.wait()
            assert op2.result == "second"
        finally:
            await outbox.close()

    @pytest.mark.anyio
    async def test_coalesced_op_preserves_queued_at(self):
        """Coalesced op inherits original queued_at for fair ordering."""
        gate = anyio.Event()

        async def _blocked():
            await gate.wait()
            return "done"

        outbox = DiscordOutbox(interval_for_channel=lambda _: 0)
        try:
            blocking_op = _make_op(execute=_blocked)
            await outbox.enqueue(key="blocker", op=blocking_op, wait=False)
            await anyio.sleep(0)

            op1 = _make_op(result_value="v1", queued_at=10.0)
            op2 = _make_op(result_value="v2", queued_at=20.0)

            await outbox.enqueue(key="same", op=op1, wait=False)
            await outbox.enqueue(key="same", op=op2, wait=False)

            # op2 should have inherited op1's queued_at
            assert op2.queued_at == 10.0

            gate.set()
            await op2.done.wait()
        finally:
            await outbox.close()


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


class TestPriorityOrdering:
    @pytest.mark.anyio
    async def test_pick_locked_prefers_higher_priority(self):
        """Lower priority number is picked first."""
        outbox = DiscordOutbox()
        outbox._pending["edit"] = _make_op(priority=EDIT_PRIORITY, queued_at=0.0)
        outbox._pending["send"] = _make_op(priority=SEND_PRIORITY, queued_at=1.0)
        outbox._pending["delete"] = _make_op(priority=DELETE_PRIORITY, queued_at=0.5)

        key, op = outbox._pick_locked()
        assert key == "send"
        assert op.priority == SEND_PRIORITY

    @pytest.mark.anyio
    async def test_pick_locked_tiebreaks_by_queued_at(self):
        """Same priority, earlier queued_at wins."""
        outbox = DiscordOutbox()
        outbox._pending["late"] = _make_op(priority=SEND_PRIORITY, queued_at=5.0)
        outbox._pending["early"] = _make_op(priority=SEND_PRIORITY, queued_at=1.0)

        key, op = outbox._pick_locked()
        assert key == "early"
        assert op.queued_at == 1.0

    @pytest.mark.anyio
    async def test_pick_locked_empty_returns_none(self):
        outbox = DiscordOutbox()
        assert outbox._pick_locked() is None

    @pytest.mark.anyio
    async def test_execution_order_by_priority(self):
        """Ops enqueued together execute in priority order."""
        gate = anyio.Event()
        execution_order: list[str] = []

        async def _blocked():
            await gate.wait()
            return "blocker"

        async def _track(name: str):
            execution_order.append(name)
            return name

        outbox = DiscordOutbox(interval_for_channel=lambda _: 0)
        try:
            # Block the worker
            blocking_op = _make_op(execute=_blocked)
            await outbox.enqueue(key="blocker", op=blocking_op, wait=False)
            await anyio.sleep(0)

            # Enqueue in reverse priority order
            edit_op = _make_op(
                execute=lambda: _track("edit"),
                priority=EDIT_PRIORITY,
                queued_at=0.0,
            )
            delete_op = _make_op(
                execute=lambda: _track("delete"),
                priority=DELETE_PRIORITY,
                queued_at=0.0,
            )
            send_op = _make_op(
                execute=lambda: _track("send"),
                priority=SEND_PRIORITY,
                queued_at=0.0,
            )

            await outbox.enqueue(key="edit", op=edit_op, wait=False)
            await outbox.enqueue(key="delete", op=delete_op, wait=False)
            await outbox.enqueue(key="send", op=send_op, wait=False)

            # Release the blocker
            gate.set()

            # Wait for all ops
            await edit_op.done.wait()
            await delete_op.done.wait()
            await send_op.done.wait()

            assert execution_order == ["send", "delete", "edit"]
        finally:
            await outbox.close()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    @pytest.mark.anyio
    async def test_rate_limit_interval_applied(self):
        """After executing, next_at is set based on channel interval."""
        clock = FakeClock(100.0)
        sleep = FakeSleep(clock)

        outbox = DiscordOutbox(
            interval_for_channel=lambda _: 0.5,
            clock=clock,
            sleep=sleep,
        )
        try:
            op = _make_op(result_value="r", channel_id=42)
            result = await outbox.enqueue(key="k", op=op)
            assert result == "r"
            assert outbox.next_at == 100.0 + 0.5
        finally:
            await outbox.close()

    @pytest.mark.anyio
    async def test_per_channel_interval(self):
        """interval_for_channel callback receives the op's channel_id."""
        received_channels: list[int | None] = []

        def _interval(ch):
            received_channels.append(ch)
            return 0

        outbox = DiscordOutbox(interval_for_channel=_interval)
        try:
            op = _make_op(channel_id=123)
            await outbox.enqueue(key="k", op=op)
            assert received_channels == [123]
        finally:
            await outbox.close()

    @pytest.mark.anyio
    async def test_default_interval_used_when_no_callback(self):
        """Without interval_for_channel, DEFAULT_CHANNEL_INTERVAL is used."""
        clock = FakeClock(0.0)
        sleep = FakeSleep(clock)

        outbox = DiscordOutbox(clock=clock, sleep=sleep)
        try:
            op = _make_op(result_value="x")
            await outbox.enqueue(key="k", op=op)
            assert outbox.next_at == pytest.approx(DEFAULT_CHANNEL_INTERVAL)
        finally:
            await outbox.close()

    @pytest.mark.anyio
    async def test_sleep_until_skips_when_past(self):
        """_sleep_until does nothing if deadline already passed."""
        clock = FakeClock(10.0)
        sleep = FakeSleep(clock)
        outbox = DiscordOutbox(clock=clock, sleep=sleep)
        await outbox._sleep_until(5.0)
        assert len(sleep.calls) == 0

    @pytest.mark.anyio
    async def test_sleep_until_sleeps_correct_amount(self):
        clock = FakeClock(10.0)
        sleep = FakeSleep(clock)
        outbox = DiscordOutbox(clock=clock, sleep=sleep)
        await outbox._sleep_until(12.5)
        assert sleep.calls == [2.5]


# ---------------------------------------------------------------------------
# RetryAfter handling
# ---------------------------------------------------------------------------


class TestRetryAfterHandling:
    @pytest.mark.anyio
    async def test_retry_after_requeues_and_succeeds(self):
        call_count = 0

        async def _flaky():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RetryAfter(0.01)
            return "ok"

        clock = FakeClock()
        sleep = FakeSleep(clock)

        outbox = DiscordOutbox(
            interval_for_channel=lambda _: 0,
            clock=clock,
            sleep=sleep,
        )
        try:
            op = OutboxOp(
                execute=_flaky,
                priority=SEND_PRIORITY,
                queued_at=0.0,
                channel_id=None,
            )
            result = await outbox.enqueue(key="k", op=op)
            assert result == "ok"
            assert call_count == 2
        finally:
            await outbox.close()

    @pytest.mark.anyio
    async def test_retry_after_sets_retry_at(self):
        """retry_at is updated when RetryAfter is raised."""
        call_count = 0

        async def _flaky():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RetryAfter(5.0)
            return "done"

        clock = FakeClock(100.0)
        sleep = FakeSleep(clock)

        outbox = DiscordOutbox(
            interval_for_channel=lambda _: 0,
            clock=clock,
            sleep=sleep,
        )
        try:
            op = OutboxOp(
                execute=_flaky,
                priority=SEND_PRIORITY,
                queued_at=0.0,
                channel_id=None,
            )
            result = await outbox.enqueue(key="k", op=op)
            assert result == "done"
            assert outbox.retry_at >= 105.0
        finally:
            await outbox.close()

    @pytest.mark.anyio
    async def test_retry_after_coalesced_during_retry(self):
        """If a new op replaces the retried op, retried op gets None."""
        call_count = 0
        gate = anyio.Event()

        async def _flaky():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RetryAfter(0.0)
            return "retry_result"

        async def _replacement():
            return "replaced"

        outbox = DiscordOutbox(interval_for_channel=lambda _: 0)
        try:
            op1 = OutboxOp(
                execute=_flaky,
                priority=SEND_PRIORITY,
                queued_at=0.0,
                channel_id=None,
            )
            # Enqueue first op (will fail with RetryAfter, then be re-queued)
            await outbox.enqueue(key="k", op=op1, wait=False)
            # Give worker time to fail and re-enqueue
            await anyio.sleep(0.05)

            # Now replace with a new op
            op2 = OutboxOp(
                execute=_replacement,
                priority=SEND_PRIORITY,
                queued_at=1.0,
                channel_id=None,
            )
            result = await outbox.enqueue(key="k", op=op2)
            assert result == "replaced"
        finally:
            await outbox.close()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.anyio
    async def test_non_retry_error_returns_none(self):
        errors: list[tuple[OutboxOp, Exception]] = []

        async def _fail():
            raise ValueError("boom")

        outbox = DiscordOutbox(
            interval_for_channel=lambda _: 0,
            on_error=lambda op, exc: errors.append((op, exc)),
        )
        try:
            op = OutboxOp(
                execute=_fail,
                priority=SEND_PRIORITY,
                queued_at=0.0,
                channel_id=None,
                label="failing_op",
            )
            result = await outbox.enqueue(key="k", op=op)
            assert result is None
            assert len(errors) == 1
            assert str(errors[0][1]) == "boom"
            assert errors[0][0].label == "failing_op"
        finally:
            await outbox.close()

    @pytest.mark.anyio
    async def test_error_without_callback_is_silent(self):
        """Errors are swallowed when no on_error callback is set."""

        async def _fail():
            raise RuntimeError("silent")

        outbox = DiscordOutbox(interval_for_channel=lambda _: 0)
        try:
            op = _make_op(execute=_fail)
            result = await outbox.enqueue(key="k", op=op)
            assert result is None
            assert op.done.is_set()
        finally:
            await outbox.close()


# ---------------------------------------------------------------------------
# Close / drop_pending
# ---------------------------------------------------------------------------


class TestCloseAndDrop:
    @pytest.mark.anyio
    async def test_close_resolves_pending_with_none(self):
        """Close resolves pending ops with None."""
        outbox = DiscordOutbox(interval_for_channel=lambda _: 10)  # long interval
        try:
            op = _make_op(result_value="first")
            # Execute first op to set next_at far in the future
            await outbox.enqueue(key="k1", op=op)
        except Exception:
            pass

        # Now enqueue another op that will be pending due to rate limit
        pending_op = _make_op(result_value="pending")
        await outbox.enqueue(key="k2", op=pending_op, wait=False)

        await outbox.close()

        assert pending_op.done.is_set()
        assert pending_op.result is None

    @pytest.mark.anyio
    async def test_enqueue_after_close_returns_none(self):
        outbox = DiscordOutbox(interval_for_channel=lambda _: 0)
        await outbox.ensure_worker()
        await outbox.close()

        op = _make_op(result_value="late")
        result = await outbox.enqueue(key="k", op=op)
        assert result is None
        assert op.done.is_set()

    @pytest.mark.anyio
    async def test_drop_pending_removes_op(self):
        outbox = DiscordOutbox(interval_for_channel=lambda _: 10)  # long interval
        try:
            # Execute first op to set next_at far in the future
            first_op = _make_op(result_value="first")
            await outbox.enqueue(key="k1", op=first_op)

            # Enqueue op that will be pending due to rate limit
            pending_op = _make_op(result_value="to_drop")
            await outbox.enqueue(key="drop_me", op=pending_op, wait=False)

            await outbox.drop_pending(key="drop_me")
            assert pending_op.done.is_set()
            assert pending_op.result is None
        finally:
            await outbox.close()

    @pytest.mark.anyio
    async def test_drop_pending_nonexistent_key_is_noop(self):
        outbox = DiscordOutbox(interval_for_channel=lambda _: 0)
        try:
            await outbox.ensure_worker()
            # Should not raise
            await outbox.drop_pending(key="nonexistent")
        finally:
            await outbox.close()

    @pytest.mark.anyio
    async def test_close_idempotent(self):
        outbox = DiscordOutbox(interval_for_channel=lambda _: 0)
        await outbox.ensure_worker()
        await outbox.close()
        # Second close should not raise
        await outbox.close()


# ---------------------------------------------------------------------------
# ensure_worker
# ---------------------------------------------------------------------------


class TestEnsureWorker:
    @pytest.mark.anyio
    async def test_ensure_worker_creates_task_group(self):
        outbox = DiscordOutbox(interval_for_channel=lambda _: 0)
        assert outbox._tg is None
        await outbox.ensure_worker()
        assert outbox._tg is not None
        await outbox.close()

    @pytest.mark.anyio
    async def test_ensure_worker_idempotent(self):
        outbox = DiscordOutbox(interval_for_channel=lambda _: 0)
        await outbox.ensure_worker()
        tg = outbox._tg
        await outbox.ensure_worker()
        assert outbox._tg is tg  # same task group
        await outbox.close()

    @pytest.mark.anyio
    async def test_ensure_worker_noop_after_close(self):
        outbox = DiscordOutbox(interval_for_channel=lambda _: 0)
        await outbox.ensure_worker()
        await outbox.close()
        assert outbox._tg is None
        await outbox.ensure_worker()
        assert outbox._tg is None  # still None after close


# ---------------------------------------------------------------------------
# Fatal outbox error
# ---------------------------------------------------------------------------


class TestFatalError:
    @pytest.mark.anyio
    async def test_on_outbox_error_called_on_fatal(self):
        """Fatal exception in run() triggers on_outbox_error and closes outbox."""
        fatal_errors: list[Exception] = []

        # We can trigger a fatal error by making _pick_locked raise
        outbox = DiscordOutbox(
            interval_for_channel=lambda _: 0,
            on_outbox_error=lambda exc: fatal_errors.append(exc),
        )
        await outbox.ensure_worker()

        # Monkey-patch to cause a fatal error during the run loop
        original_pick = outbox._pick_locked

        def _exploding_pick():
            raise RuntimeError("fatal!")

        outbox._pick_locked = _exploding_pick

        # Enqueue to trigger the worker to pick
        op = _make_op(result_value="x")
        async with outbox._cond:
            outbox._pending["k"] = op
            outbox._cond.notify()

        await op.done.wait()
        assert op.result is None  # failed pending
        assert outbox._closed

        # Give worker a moment to call on_outbox_error
        await anyio.sleep(0.05)
        assert len(fatal_errors) == 1
        assert "fatal!" in str(fatal_errors[0])

        # Cleanup (already closed internally)
        outbox._tg = None
