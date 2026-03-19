"""Direct unit tests for core/lifecycle.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio

from tunapi.core.lifecycle import (
    cleanup_heartbeat,
    detect_abnormal_termination,
    graceful_drain,
    save_shutdown_state,
    send_restart_notification,
)


class TestDetectAbnormalTermination:
    def test_no_heartbeat_file(self, tmp_path: Path):
        """No heartbeat file → no detection."""

        async def _run():
            await detect_abnormal_termination(
                heartbeat_path=tmp_path / "heartbeat",
                shutdown_state_path=tmp_path / "shutdown.json",
                log_prefix="test",
            )

        anyio.run(_run)
        # Should not raise

    def test_shutdown_state_exists(self, tmp_path: Path):
        """Shutdown state present → not abnormal (graceful exit)."""
        hb = tmp_path / "heartbeat"
        sd = tmp_path / "shutdown.json"
        hb.write_text((datetime.now(tz=UTC) - timedelta(minutes=5)).isoformat())
        sd.write_text("{}")

        async def _run():
            await detect_abnormal_termination(
                heartbeat_path=hb,
                shutdown_state_path=sd,
                log_prefix="test",
            )

        anyio.run(_run)

    def test_stale_heartbeat_no_shutdown(self, tmp_path: Path):
        """Stale heartbeat + no shutdown state → abnormal (logs warning, no crash)."""
        hb = tmp_path / "heartbeat"
        sd = tmp_path / "shutdown.json"
        hb.write_text((datetime.now(tz=UTC) - timedelta(minutes=5)).isoformat())
        # No shutdown state file

        async def _run():
            await detect_abnormal_termination(
                heartbeat_path=hb,
                shutdown_state_path=sd,
                log_prefix="test",
            )

        anyio.run(_run)

    def test_fresh_heartbeat_no_shutdown(self, tmp_path: Path):
        """Fresh heartbeat + no shutdown → not stale (within threshold)."""
        hb = tmp_path / "heartbeat"
        sd = tmp_path / "shutdown.json"
        hb.write_text(datetime.now(tz=UTC).isoformat())

        async def _run():
            await detect_abnormal_termination(
                heartbeat_path=hb,
                shutdown_state_path=sd,
                log_prefix="test",
            )

        anyio.run(_run)

    def test_naive_timestamp_compat(self, tmp_path: Path):
        """Naive timestamps (no tzinfo) should be handled without error."""
        hb = tmp_path / "heartbeat"
        sd = tmp_path / "shutdown.json"
        hb.write_text(
            (datetime.now() - timedelta(minutes=5)).isoformat()  # noqa: DTZ005
        )

        async def _run():
            await detect_abnormal_termination(
                heartbeat_path=hb,
                shutdown_state_path=sd,
                log_prefix="test",
            )

        anyio.run(_run)


class TestSaveShutdownState:
    def test_save_sigterm(self, tmp_path: Path):
        path = tmp_path / "shutdown.json"
        save_shutdown_state(
            shutdown_state_path=path,
            is_sigterm=True,
            running_task_count=3,
        )
        data = json.loads(path.read_text())
        assert data["reason"] == "SIGTERM"
        assert data["running_tasks"] == 3
        assert "timestamp" in data

    def test_save_disconnect(self, tmp_path: Path):
        path = tmp_path / "shutdown.json"
        save_shutdown_state(
            shutdown_state_path=path,
            is_sigterm=False,
            running_task_count=0,
        )
        data = json.loads(path.read_text())
        assert data["reason"] == "disconnect"


class TestSendRestartNotification:
    def test_no_file(self, tmp_path: Path):
        """No shutdown state file → no notification."""
        sent: list[tuple[str, str]] = []

        async def send_fn(ch: str, msg: str) -> None:
            sent.append((ch, msg))

        async def _run():
            await send_restart_notification(
                shutdown_state_path=tmp_path / "nonexistent.json",
                channel_id="C123",
                send_fn=send_fn,
            )

        anyio.run(_run)
        assert not sent

    def test_sends_notification(self, tmp_path: Path):
        """Reads shutdown state and sends notification."""
        path = tmp_path / "shutdown.json"
        path.write_text(
            json.dumps(
                {
                    "reason": "SIGTERM",
                    "running_tasks": 2,
                    "timestamp": "2026-03-20 05:00:00",
                }
            )
        )
        sent: list[tuple[str, str]] = []

        async def send_fn(ch: str, msg: str) -> None:
            sent.append((ch, msg))

        async def _run():
            await send_restart_notification(
                shutdown_state_path=path,
                channel_id="C123",
                send_fn=send_fn,
            )

        anyio.run(_run)
        assert len(sent) == 1
        assert "SIGTERM" in sent[0][1]
        assert "2" in sent[0][1]
        assert not path.exists()  # cleaned up

    def test_no_channel_id(self, tmp_path: Path):
        """channel_id=None → no send, but file still cleaned up."""
        path = tmp_path / "shutdown.json"
        path.write_text(json.dumps({"reason": "SIGTERM", "running_tasks": 0}))
        sent: list[tuple[str, str]] = []

        async def send_fn(ch: str, msg: str) -> None:
            sent.append((ch, msg))

        async def _run():
            await send_restart_notification(
                shutdown_state_path=path,
                channel_id=None,
                send_fn=send_fn,
            )

        anyio.run(_run)
        assert not sent
        assert not path.exists()


class TestCleanupHeartbeat:
    def test_removes_file(self, tmp_path: Path):
        hb = tmp_path / "heartbeat"
        hb.write_text("test")
        cleanup_heartbeat(hb)
        assert not hb.exists()

    def test_missing_file_ok(self, tmp_path: Path):
        cleanup_heartbeat(tmp_path / "nonexistent")  # should not raise


class TestGracefulDrain:
    def test_empty_tasks(self):
        """Empty dict → immediate return."""

        async def _run():
            await graceful_drain({}, log_prefix="test")

        anyio.run(_run)

    def test_waits_for_done(self):
        """Waits for task.done events."""
        done = anyio.Event()

        class FakeTask:
            def __init__(self):
                self.done = done

        tasks = {"ref1": FakeTask()}

        async def _run():
            async with anyio.create_task_group() as tg:

                async def _set_done():
                    await anyio.sleep(0.1)
                    done.set()

                tg.start_soon(_set_done)
                await graceful_drain(tasks, log_prefix="test")

        anyio.run(_run)
