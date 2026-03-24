"""Tests for journal.py — JSONL journal and handoff preamble."""

from __future__ import annotations

import time

import pytest

from tunapi.journal import (
    Journal,
    JournalEntry,
    PendingRun,
    PendingRunLedger,
    _sanitize_channel_id,
    _truncate,
    build_handoff_preamble,
    make_run_id,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


class TestSanitizeChannelId:
    def test_no_special_chars(self):
        assert _sanitize_channel_id("abc123") == "abc123"

    def test_forward_slash(self):
        assert _sanitize_channel_id("branch:a/b") == "branch:a_b"

    def test_backslash(self):
        assert _sanitize_channel_id("a\\b") == "a_b"

    def test_dotdot(self):
        # ".." is replaced as a single token, not char-by-char
        assert _sanitize_channel_id("..evil") == "_evil"


class TestTruncate:
    def test_none_returns_empty(self):
        assert _truncate(None) == ""

    def test_short_text_unchanged(self):
        assert _truncate("hello") == "hello"

    def test_exact_limit(self):
        text = "a" * 2048
        assert _truncate(text) == text

    def test_over_limit_truncated(self):
        text = "a" * 3000
        result = _truncate(text)
        assert result.endswith("...")
        assert len(result) == 2048 + 3

    def test_custom_max_len(self):
        result = _truncate("abcdef", max_len=3)
        assert result == "abc..."


class TestMakeRunId:
    def test_contains_channel_and_message(self):
        rid = make_run_id("ch1", "msg42")
        assert rid.startswith("ch1:msg42:")
        # Third part is timestamp
        ts_part = rid.split(":")[2]
        assert ts_part.isdigit()


# ---------------------------------------------------------------------------
# Journal append / read
# ---------------------------------------------------------------------------


def _entry(
    channel_id: str = "ch1",
    run_id: str = "run1",
    event: str = "prompt",
    engine: str | None = "claude",
    data: dict | None = None,
) -> JournalEntry:
    return JournalEntry(
        run_id=run_id,
        channel_id=channel_id,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
        event=event,
        engine=engine,
        data=data or {},
    )


class TestJournalAppendRead:
    async def test_append_and_read(self, tmp_path):
        j = Journal(tmp_path / "journals")
        e = _entry(data={"text": "hello"})
        await j.append(e)

        entries = await j.recent_entries("ch1")
        assert len(entries) == 1
        assert entries[0].data["text"] == "hello"

    async def test_read_nonexistent_channel(self, tmp_path):
        j = Journal(tmp_path / "journals")
        entries = await j.recent_entries("no_such_channel")
        assert entries == []

    async def test_limit_respected(self, tmp_path):
        j = Journal(tmp_path / "journals")
        for i in range(10):
            await j.append(_entry(run_id=f"run{i}"))

        entries = await j.recent_entries("ch1", limit=3)
        assert len(entries) == 3
        # Should be the LAST 3
        assert entries[0].run_id == "run7"

    async def test_multiple_channels_isolated(self, tmp_path):
        j = Journal(tmp_path / "journals")
        await j.append(_entry(channel_id="ch1", run_id="r1"))
        await j.append(_entry(channel_id="ch2", run_id="r2"))

        assert len(await j.recent_entries("ch1")) == 1
        assert len(await j.recent_entries("ch2")) == 1


class TestJournalRotation:
    async def test_rotation_trims_old_entries(self, tmp_path):
        """When entry count exceeds _MAX_ENTRIES, older half is trimmed."""
        j = Journal(tmp_path / "journals")
        # Write enough entries to trigger rotation (> 500)
        for i in range(510):
            await j.append(
                JournalEntry(
                    run_id=f"r{i}",
                    channel_id="ch1",
                    timestamp=f"2026-01-01T00:00:{i % 60:02d}",
                    event="prompt",
                    data={"i": i},
                )
            )

        entries = await j.recent_entries("ch1", limit=600)
        # After rotation, should have roughly half
        assert len(entries) < 510
        assert len(entries) >= 250


class TestJournalLastRun:
    async def test_last_run_returns_entries_for_latest_run_id(self, tmp_path):
        j = Journal(tmp_path / "journals")
        await j.append(_entry(run_id="r1", event="prompt"))
        await j.append(_entry(run_id="r1", event="completed"))
        await j.append(_entry(run_id="r2", event="prompt"))
        await j.append(_entry(run_id="r2", event="action"))
        await j.append(_entry(run_id="r2", event="completed"))

        result = await j.last_run("ch1")
        assert result is not None
        assert all(e.run_id == "r2" for e in result)
        assert len(result) == 3

    async def test_last_run_empty_channel(self, tmp_path):
        j = Journal(tmp_path / "journals")
        assert await j.last_run("ch1") is None


class TestJournalMarkInterrupted:
    async def test_mark_interrupted(self, tmp_path):
        j = Journal(tmp_path / "journals")
        await j.mark_interrupted("ch1", "run1", "cancelled")

        entries = await j.recent_entries("ch1")
        assert len(entries) == 1
        assert entries[0].event == "interrupted"
        assert entries[0].data["reason"] == "cancelled"


class TestJournalMarkReset:
    async def test_mark_reset(self, tmp_path):
        j = Journal(tmp_path / "journals")
        await j.mark_reset("ch1")

        entries = await j.recent_entries("ch1")
        assert len(entries) == 1
        assert entries[0].event == "reset"
        assert entries[0].run_id == "reset"


class TestJournalGlobal:
    async def test_recent_entries_global(self, tmp_path):
        j = Journal(tmp_path / "journals")
        await j.append(_entry(channel_id="ch1", run_id="r1"))
        await j.append(_entry(channel_id="ch2", run_id="r2"))

        entries = await j.recent_entries_global()
        assert len(entries) == 2


class TestJournalForProject:
    async def test_entries_for_project_across_channels(self, tmp_path):
        j = Journal(tmp_path / "journals")
        await j.append(_entry(channel_id="ch1", run_id="r1"))
        await j.append(_entry(channel_id="ch2", run_id="r2"))
        await j.append(_entry(channel_id="ch3", run_id="r3"))

        entries = await j.recent_entries_for_project(["ch1", "ch2"])
        assert len(entries) == 2

    async def test_entries_for_project_extra_dirs(self, tmp_path):
        j1 = Journal(tmp_path / "j1")
        j2_dir = tmp_path / "j2"
        j2_dir.mkdir()

        await j1.append(_entry(channel_id="ch1", run_id="r1"))
        # Write directly to j2_dir
        j2 = Journal(j2_dir)
        await j2.append(_entry(channel_id="ch1", run_id="r2"))

        entries = await j1.recent_entries_for_project(
            ["ch1"], extra_journal_dirs=[j2_dir]
        )
        assert len(entries) == 2


# ---------------------------------------------------------------------------
# Handoff preamble
# ---------------------------------------------------------------------------


class TestBuildHandoffPreamble:
    def test_empty_entries(self):
        assert build_handoff_preamble([]) is None

    def test_reset_marker_returns_none(self):
        entries = [_entry(event="reset", run_id="reset")]
        assert build_handoff_preamble(entries) is None

    def test_basic_preamble(self):
        entries = [
            _entry(run_id="r1", event="prompt", data={"text": "fix the bug"}),
            _entry(run_id="r1", event="action", data={"kind": "edit", "title": "main.py"}),
            _entry(run_id="r1", event="completed", data={"answer": "Fixed!", "ok": True}),
        ]
        result = build_handoff_preamble(entries, old_engine="claude")
        assert result is not None
        assert "fix the bug" in result
        assert "edit: main.py" in result
        assert "Fixed!" in result
        assert "완료" in result

    def test_interrupted_status(self):
        entries = [
            _entry(run_id="r1", event="prompt", data={"text": "test"}),
            _entry(run_id="r1", event="interrupted", data={"reason": "user cancel"}),
        ]
        result = build_handoff_preamble(entries, old_engine="claude")
        assert "중단" in result
        assert "user cancel" in result

    def test_engine_change_reason(self):
        entries = [_entry(run_id="r1", event="prompt", data={"text": "hi"})]
        result = build_handoff_preamble(entries, reason="engine_change")
        assert "엔진 변경" in result

    def test_context_overflow_reason(self):
        entries = [_entry(run_id="r1", event="prompt", data={"text": "hi"})]
        result = build_handoff_preamble(entries, reason="context_overflow")
        assert "컨텍스트 초과" in result

    def test_resume_expired_reason(self):
        entries = [_entry(run_id="r1", event="prompt", data={"text": "hi"})]
        result = build_handoff_preamble(entries, reason="resume_expired")
        assert "세션 만료" in result

    def test_reset_after_prompt_returns_none(self):
        entries = [
            _entry(run_id="r1", event="prompt", data={"text": "old"}),
            _entry(run_id="reset", event="reset"),
        ]
        assert build_handoff_preamble(entries) is None

    def test_multiple_runs(self):
        entries = [
            _entry(run_id="r1", event="prompt", data={"text": "first"}),
            _entry(run_id="r1", event="completed", data={"answer": "done1", "ok": True}),
            _entry(run_id="r2", event="prompt", data={"text": "second"}),
            _entry(run_id="r2", event="completed", data={"answer": "done2", "ok": True}),
        ]
        result = build_handoff_preamble(entries, old_engine="claude")
        assert "first" in result
        assert "second" in result

    def test_many_actions_capped(self):
        entries = [_entry(run_id="r1", event="prompt", data={"text": "work"})]
        for i in range(10):
            entries.append(
                _entry(
                    run_id="r1",
                    event="action",
                    data={"kind": "edit", "title": f"file{i}.py"},
                )
            )
        entries.append(
            _entry(run_id="r1", event="completed", data={"answer": "done", "ok": True})
        )
        result = build_handoff_preamble(entries)
        # Should show 5 actions + "외 5개"
        assert "외 5개" in result


# ---------------------------------------------------------------------------
# PendingRunLedger
# ---------------------------------------------------------------------------


class TestPendingRunLedger:
    async def test_register_and_get(self, tmp_path):
        ledger = PendingRunLedger(tmp_path / "pending.json")
        run = PendingRun(
            run_id="r1",
            channel_id="ch1",
            engine="claude",
            prompt_summary="fix bug",
            started_at="2026-01-01T00:00:00",
        )
        await ledger.register(run)
        runs = await ledger.get_all()
        assert len(runs) == 1
        assert runs[0].run_id == "r1"

    async def test_complete_removes(self, tmp_path):
        ledger = PendingRunLedger(tmp_path / "pending.json")
        run = PendingRun(
            run_id="r1",
            channel_id="ch1",
            engine="claude",
            prompt_summary="test",
            started_at="2026-01-01T00:00:00",
        )
        await ledger.register(run)
        await ledger.complete("r1")
        assert await ledger.get_all() == []

    async def test_complete_nonexistent_noop(self, tmp_path):
        ledger = PendingRunLedger(tmp_path / "pending.json")
        await ledger.complete("no_such_run")  # Should not raise

    async def test_clear_all(self, tmp_path):
        ledger = PendingRunLedger(tmp_path / "pending.json")
        for i in range(3):
            await ledger.register(
                PendingRun(
                    run_id=f"r{i}",
                    channel_id="ch1",
                    engine="claude",
                    prompt_summary=f"task{i}",
                    started_at="2026-01-01T00:00:00",
                )
            )
        await ledger.clear_all()
        assert await ledger.get_all() == []

    async def test_persistence(self, tmp_path):
        path = tmp_path / "pending.json"
        ledger1 = PendingRunLedger(path)
        await ledger1.register(
            PendingRun(
                run_id="r1",
                channel_id="ch1",
                engine="claude",
                prompt_summary="persist test",
                started_at="2026-01-01T00:00:00",
            )
        )
        # New instance, same path
        ledger2 = PendingRunLedger(path)
        runs = await ledger2.get_all()
        assert len(runs) == 1
        assert runs[0].prompt_summary == "persist test"

    async def test_corrupted_file_recovery(self, tmp_path):
        path = tmp_path / "pending.json"
        path.write_text("not valid json")
        ledger = PendingRunLedger(path)
        runs = await ledger.get_all()
        assert runs == []
