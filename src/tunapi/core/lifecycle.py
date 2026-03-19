"""Shared lifecycle utilities for WebSocket-based transports.

Provides heartbeat, shutdown state, restart notification, pending-run
recovery, and graceful drain — extracted from Mattermost/Slack loop.py.
"""

from __future__ import annotations

import contextlib
import json
import signal
import time as _time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import anyio

from ..journal import Journal, PendingRunLedger
from ..logging import get_logger

logger = get_logger(__name__)

_HEARTBEAT_INTERVAL = 10  # seconds
_HEARTBEAT_STALE_THRESHOLD = 30  # seconds
_DRAIN_TIMEOUT = 30  # seconds


async def detect_abnormal_termination(
    *,
    heartbeat_path: Path,
    shutdown_state_path: Path,
    log_prefix: str,
) -> None:
    """Log a warning if the previous process exited without saving shutdown state."""
    if not heartbeat_path.exists() or shutdown_state_path.exists():
        return
    with contextlib.suppress(Exception):
        last_beat = datetime.fromisoformat(heartbeat_path.read_text().strip())
        now = datetime.now(tz=UTC)
        if last_beat.tzinfo is None:
            last_beat = last_beat.replace(tzinfo=UTC)
        age = (now - last_beat).total_seconds()
        if age > _HEARTBEAT_STALE_THRESHOLD:
            logger.warning(
                f"{log_prefix}.abnormal_termination_detected",
                last_heartbeat_age_s=round(age),
            )


async def send_restart_notification(
    *,
    shutdown_state_path: Path,
    channel_id: str | None,
    send_fn: Callable[[str, str], Awaitable[None]],
) -> None:
    """Read previous shutdown state and notify the channel, then clean up."""
    if not shutdown_state_path.exists():
        return
    if channel_id is not None:
        with contextlib.suppress(Exception):
            state = json.loads(shutdown_state_path.read_text())
            reason = state.get("reason", "unknown")
            tasks = state.get("running_tasks", 0)
            ts = state.get("timestamp", "")
            parts = [f"🔄 서비스 재시작 완료 (이전 종료: {reason})"]
            if tasks > 0:
                parts.append(
                    f"⚠️ 종료 시 진행 중이던 작업 {tasks}개가 중단되었을 수 있습니다."
                )
            if ts:
                parts.append(f"종료 시각: {ts}")
            await send_fn(channel_id, "\n".join(parts))
    shutdown_state_path.unlink(missing_ok=True)


async def recover_pending_runs(
    *,
    journal: Journal,
    ledger: PendingRunLedger,
    send_fn: Callable[[str, str], Awaitable[None]],
) -> None:
    """Mark pending runs as interrupted and notify affected channels."""
    with contextlib.suppress(Exception):
        pending = await ledger.get_all()
        if not pending:
            return
        from itertools import groupby
        from operator import attrgetter

        sorted_pending = sorted(pending, key=attrgetter("channel_id"))
        for ch_id, runs in groupby(sorted_pending, key=attrgetter("channel_id")):
            run_list = list(runs)
            for run in run_list:
                await journal.mark_interrupted(run.channel_id, run.run_id, "crash")
            await send_fn(
                ch_id,
                f"⚠️ 이전 세션에서 중단된 작업 {len(run_list)}개가 있습니다.",
            )
        await ledger.clear_all()


def register_sigterm_handler(shutdown: anyio.Event, *, log_prefix: str) -> None:
    """Register a SIGTERM handler that sets the shutdown event."""

    def _on_sigterm(*_: object) -> None:
        logger.info(f"{log_prefix}.sigterm_received")
        shutdown.set()

    with contextlib.suppress(OSError, ValueError):
        signal.signal(signal.SIGTERM, _on_sigterm)


async def heartbeat_loop(heartbeat_path: Path) -> None:
    """Write a UTC timestamp to *heartbeat_path* every 10 seconds."""
    while True:
        with contextlib.suppress(Exception):
            heartbeat_path.write_text(datetime.now(tz=UTC).isoformat())
        await anyio.sleep(_HEARTBEAT_INTERVAL)


async def graceful_drain(running_tasks: dict, *, log_prefix: str) -> None:
    """Wait for running tasks to complete (max 30s)."""
    if not running_tasks:
        return
    logger.info(f"{log_prefix}.draining_tasks", count=len(running_tasks))
    with anyio.move_on_after(_DRAIN_TIMEOUT):
        for task in list(running_tasks.values()):
            await task.done.wait()
    logger.info(f"{log_prefix}.drain_complete")


def save_shutdown_state(
    *,
    shutdown_state_path: Path,
    is_sigterm: bool,
    running_task_count: int,
) -> None:
    """Persist shutdown state for restart notification."""
    reason = "SIGTERM" if is_sigterm else "disconnect"
    with contextlib.suppress(Exception):
        shutdown_state_path.parent.mkdir(parents=True, exist_ok=True)
        shutdown_state_path.write_text(
            json.dumps(
                {
                    "reason": reason,
                    "running_tasks": running_task_count,
                    "timestamp": _time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        )


def cleanup_heartbeat(heartbeat_path: Path) -> None:
    """Remove heartbeat file on graceful exit."""
    heartbeat_path.unlink(missing_ok=True)
