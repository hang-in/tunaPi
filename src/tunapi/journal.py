"""Structured JSONL journal for conversation history.

Records prompts, actions, and completions per channel so that handoff
preambles can be constructed when resume tokens are unavailable.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Any

import anyio
import msgspec

from .logging import get_logger

logger = get_logger(__name__)

_MAX_ENTRIES = 500
_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
_TRUNCATE_TEXT = 2048  # max chars for prompt/answer text


def _sanitize_channel_id(channel_id: str) -> str:
    """Make channel_id safe for use as a filename."""
    return channel_id.replace("/", "_").replace("\\", "_").replace("..", "_")


def _truncate(text: str | None, max_len: int = _TRUNCATE_TEXT) -> str:
    if text is None:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


class JournalEntry(msgspec.Struct):
    """A single journal record."""

    run_id: str
    channel_id: str
    timestamp: str
    event: (
        str  # "prompt" | "started" | "action" | "completed" | "interrupted" | "reset"
    )
    engine: str | None = None
    data: dict[str, Any] = msgspec.field(default_factory=dict)


_encoder = msgspec.json.Encoder()
_decoder = msgspec.json.Decoder(JournalEntry)


class Journal:
    """Append-only JSONL journal with per-channel files.

    Files are stored under ``base_dir/{channel_id}.jsonl``.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = anyio.Lock()
        self._append_failures: int = 0

    def _path_for(self, channel_id: str) -> Path:
        return self._base_dir / f"{_sanitize_channel_id(channel_id)}.jsonl"

    async def append(self, entry: JournalEntry) -> None:
        """Append an entry. Auto-rotates if size limits are exceeded."""
        path = self._path_for(entry.channel_id)
        line = _encoder.encode(entry) + b"\n"
        async with self._lock:
            try:
                async with await anyio.open_file(path, "ab") as f:
                    await f.write(line)
                # Check limits
                await self._maybe_rotate(path)
                self._append_failures = 0
            except Exception as exc:  # noqa: BLE001
                self._append_failures += 1
                log_fn = logger.error if self._append_failures >= 3 else logger.warning
                log_fn(
                    "journal.append_failed",
                    channel_id=entry.channel_id,
                    error=str(exc),
                    consecutive_failures=self._append_failures,
                )

    async def _maybe_rotate(self, path: Path) -> None:
        """Trim old entries if file exceeds size/entry limits."""
        try:
            stat = await anyio.Path(path).stat()
        except FileNotFoundError:
            return
        if stat.st_size <= _MAX_BYTES:
            # Quick size check — if under limit, count lines
            async with await anyio.open_file(path, "rb") as f:
                lines = await f.readlines()
            if len(lines) <= _MAX_ENTRIES:
                return
        else:
            async with await anyio.open_file(path, "rb") as f:
                lines = await f.readlines()

        # Trim: keep the newer half
        keep = lines[len(lines) // 2 :]
        async with await anyio.open_file(path, "wb") as f:
            await f.writelines(keep)

    async def recent_entries(
        self, channel_id: str, *, limit: int = 50
    ) -> list[JournalEntry]:
        """Load the most recent *limit* entries for a channel."""
        path = self._path_for(channel_id)
        if not path.exists():
            return []
        try:
            async with await anyio.open_file(path, "rb") as f:
                lines = await f.readlines()
            entries: list[JournalEntry] = []
            for raw_line in lines[-limit:]:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                with contextlib.suppress(Exception):
                    entries.append(_decoder.decode(raw_line))
            return entries
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "journal.read_failed",
                channel_id=channel_id,
                error=str(exc),
            )
            return []

    async def recent_entries_global(self, *, limit: int = 50) -> list[JournalEntry]:
        """Load the most recent entries across ALL channels.

        Used for cross-transport handoff: when the current channel has no
        journal entries, fall back to the most recent work from any channel.
        """
        all_entries: list[JournalEntry] = []
        try:
            for path in sorted(self._base_dir.glob("*.jsonl")):
                try:
                    async with await anyio.open_file(path, "rb") as f:
                        lines = await f.readlines()
                    for raw_line in lines[-limit:]:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        with contextlib.suppress(Exception):
                            all_entries.append(_decoder.decode(raw_line))
                except Exception:  # noqa: BLE001
                    continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("journal.global_read_failed", error=str(exc))
            return []

        # Sort by timestamp and return the most recent
        all_entries.sort(key=lambda e: e.timestamp)
        return all_entries[-limit:]

    async def last_run(self, channel_id: str) -> list[JournalEntry] | None:
        """Return all entries for the most recent run_id."""
        entries = await self.recent_entries(channel_id, limit=200)
        if not entries:
            return None
        last_run_id = entries[-1].run_id
        return [e for e in entries if e.run_id == last_run_id]

    async def mark_interrupted(self, channel_id: str, run_id: str, reason: str) -> None:
        """Append an 'interrupted' entry for a run."""
        entry = JournalEntry(
            run_id=run_id,
            channel_id=channel_id,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            event="interrupted",
            data={"reason": reason},
        )
        await self.append(entry)

    async def mark_reset(self, channel_id: str) -> None:
        """Append a 'reset' marker (for /new — prevents handoff)."""
        entry = JournalEntry(
            run_id="reset",
            channel_id=channel_id,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            event="reset",
        )
        await self.append(entry)

    async def recent_entries_for_project(
        self,
        channel_ids: list[str],
        *,
        extra_journal_dirs: list[Path] | None = None,
        limit: int = 50,
    ) -> list[JournalEntry]:
        """Load recent entries across multiple channels and journal directories.

        Used for cross-transport project session analysis: a project may have
        channels in different transports (Mattermost, Slack, tunaDish) and each
        transport stores journals in a separate directory.
        """
        all_entries: list[JournalEntry] = []

        # Collect from this journal's base_dir
        for cid in channel_ids:
            entries = await self.recent_entries(cid, limit=limit)
            all_entries.extend(entries)

        # Collect from extra journal directories (other transports)
        for extra_dir in extra_journal_dirs or []:
            for cid in channel_ids:
                path = extra_dir / f"{_sanitize_channel_id(cid)}.jsonl"
                if not path.exists():
                    continue
                try:
                    async with await anyio.open_file(path, "rb") as f:
                        lines = await f.readlines()
                    for raw_line in lines[-limit:]:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        with contextlib.suppress(Exception):
                            all_entries.append(_decoder.decode(raw_line))
                except Exception:  # noqa: BLE001
                    continue

        all_entries.sort(key=lambda e: e.timestamp)
        return all_entries[-limit:]


def make_run_id(channel_id: str, message_id: str) -> str:
    """Generate a run_id from channel, message, and timestamp."""
    return f"{channel_id}:{message_id}:{int(time.time())}"


# ---------------------------------------------------------------------------
# Handoff preamble
# ---------------------------------------------------------------------------


def build_handoff_preamble(
    entries: list[JournalEntry],
    *,
    old_engine: str | None = None,
    reason: str = "engine_change",
    max_bytes: int = 4096,
) -> str | None:
    """Build a handoff preamble from journal entries.

    Returns None if there are no meaningful entries to hand off
    (e.g. last entry is a reset marker).
    """
    if not entries:
        return None

    # If the most recent entry is a reset marker, no handoff
    for e in reversed(entries):
        if e.event == "reset":
            return None
        if e.event in ("prompt", "started", "completed", "interrupted"):
            break

    # Collect data from the most recent runs (up to 3)
    runs: dict[str, list[JournalEntry]] = {}
    for e in entries:
        runs.setdefault(e.run_id, []).append(e)

    # Take the last 3 runs (most recent)
    recent_run_ids = list(runs.keys())[-3:]

    lines: list[str] = []
    reason_label = {
        "engine_change": "엔진 변경",
        "context_overflow": "컨텍스트 초과",
        "resume_expired": "세션 만료",
    }.get(reason, reason)
    lines.append(f"[이전 세션 컨텍스트 — {old_engine or 'unknown'}, {reason_label}]")

    for rid in recent_run_ids:
        run_entries = runs[rid]
        prompt_text = ""
        actions: list[str] = []
        answer_text = ""
        status = ""
        engine = old_engine

        for e in run_entries:
            if e.engine:
                engine = e.engine
            if e.event == "prompt":
                prompt_text = e.data.get("text", "")
            elif e.event == "action":
                kind = e.data.get("kind", "")
                title = e.data.get("title", "")
                if title:
                    actions.append(f"{kind}: {title}")
            elif e.event == "completed":
                answer_text = e.data.get("answer", "")
                status = "완료" if e.data.get("ok") else "에러"
            elif e.event == "interrupted":
                status = f"중단({e.data.get('reason', '')})"

        if prompt_text:
            lines.append(f"- 요청 ({engine}): {prompt_text[:200]}")
        if actions:
            # Show max 5 actions
            shown = actions[:5]
            if len(actions) > 5:
                shown.append(f"... 외 {len(actions) - 5}개")
            lines.append(f"  액션: {', '.join(shown)}")
        if answer_text:
            lines.append(f"  응답: {answer_text[:300]}")
        if status:
            lines.append(f"  상태: {status}")

    lines.append("[현재 요청]")

    text = "\n".join(lines)

    # Trim if over max_bytes
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        # Truncate by removing oldest run summaries
        while len(encoded) > max_bytes and len(recent_run_ids) > 1:
            recent_run_ids.pop(0)
            # Rebuild
            text = (
                build_handoff_preamble(
                    [
                        e
                        for e in entries
                        if e.run_id in recent_run_ids or e.run_id == "reset"
                    ],
                    old_engine=old_engine,
                    reason=reason,
                    max_bytes=max_bytes,
                )
                or ""
            )
            encoded = text.encode("utf-8")

    return text if text.strip() else None


# ---------------------------------------------------------------------------
# Pending-run ledger
# ---------------------------------------------------------------------------


class PendingRun(msgspec.Struct):
    run_id: str
    channel_id: str
    engine: str
    prompt_summary: str
    started_at: str


class _PendingRunsState(msgspec.Struct, forbid_unknown_fields=False):
    version: int = 1
    runs: dict[str, PendingRun] = msgspec.field(default_factory=dict)


class PendingRunLedger:
    """Track in-flight runs so that crashes can be detected on restart."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = anyio.Lock()
        self._state = _PendingRunsState()
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.exists():
            return
        try:
            raw = self._path.read_bytes()
            self._state = msgspec.json.decode(raw, type=_PendingRunsState)
        except Exception:  # noqa: BLE001
            self._state = _PendingRunsState()

    def _save(self) -> None:
        from .utils.json_state import atomic_write_json

        atomic_write_json(self._path, msgspec.to_builtins(self._state))

    async def register(self, run: PendingRun) -> None:
        async with self._lock:
            self._load()
            self._state.runs[run.run_id] = run
            self._save()

    async def complete(self, run_id: str) -> None:
        async with self._lock:
            self._load()
            if self._state.runs.pop(run_id, None) is not None:
                self._save()

    async def get_all(self) -> list[PendingRun]:
        async with self._lock:
            self._load()
            return list(self._state.runs.values())

    async def clear_all(self) -> None:
        async with self._lock:
            self._state = _PendingRunsState()
            self._save()
