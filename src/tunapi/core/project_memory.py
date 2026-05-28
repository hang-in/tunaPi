"""Per-project persistent memory — decisions, reviews, ideas, context.

Stores structured entries keyed by project alias.  Each project gets
its own JSON file at ``~/.tunapi/project_memory/{alias}.json``.

This module is transport-agnostic and does not touch existing
session/journal/prefs stores.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Literal, cast

import msgspec

from ..logging import get_logger
from ..state_store import JsonStateStore

logger = get_logger(__name__)

EntryType = Literal["decision", "review", "idea", "context"]


def generate_entry_id() -> str:
    """Generate a chronologically sortable, collision-resistant ID.

    Format: ``{epoch_ms}_{hex4}`` — e.g. ``1742486400000_a3f1``.
    """
    epoch_ms = int(time.time() * 1000)
    suffix = secrets.token_hex(4)
    return f"{epoch_ms}_{suffix}"


class MemoryEntry(msgspec.Struct, forbid_unknown_fields=False):
    id: str
    type: EntryType
    title: str
    content: str
    timestamp: float  # time.time()
    source: str  # engine id or "user"
    tags: list[str] = msgspec.field(default_factory=list)


class _State(msgspec.Struct, forbid_unknown_fields=False):
    version: int = 1
    entries: dict[str, MemoryEntry] = msgspec.field(default_factory=dict)


_STATE_VERSION = 1


class ProjectMemoryStore:
    """Per-project memory entry store.

    Lazily creates a :class:`JsonStateStore` per project alias.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._stores: dict[str, JsonStateStore[_State]] = {}

    def _store_for(self, project: str) -> JsonStateStore[_State]:
        store = self._stores.get(project)
        if store is None:
            path = self._base_dir / f"{project}.json"
            store = cast(
                JsonStateStore[_State],
                JsonStateStore(
                    path,
                    version=_STATE_VERSION,
                    state_type=_State,
                    state_factory=_State,
                    log_prefix="project_memory",
                    logger=logger,
                ),
            )
            self._stores[project] = store
        return store

    async def add_entry(
        self,
        project: str,
        *,
        type: EntryType,
        title: str,
        content: str,
        source: str,
        tags: list[str] | None = None,
    ) -> MemoryEntry:
        entry = MemoryEntry(
            id=generate_entry_id(),
            type=type,
            title=title,
            content=content,
            timestamp=time.time(),
            source=source,
            tags=tags or [],
        )
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            store._state.entries[entry.id] = entry
            store._save_locked()
        return entry

    async def get_entry(self, project: str, entry_id: str) -> MemoryEntry | None:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            return store._state.entries.get(entry_id)

    async def list_entries(
        self,
        project: str,
        *,
        type: EntryType | None = None,
        limit: int = 20,
    ) -> list[MemoryEntry]:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            entries = list(store._state.entries.values())
        if type is not None:
            entries = [e for e in entries if e.type == type]
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries[:limit]

    async def search(self, project: str, query: str) -> list[MemoryEntry]:
        """Simple substring search on title, content, and tags."""
        q = query.lower()
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            entries = list(store._state.entries.values())
        results = []
        for e in entries:
            haystack = f"{e.title}\n{e.content}\n{' '.join(e.tags)}".lower()
            if q in haystack:
                results.append(e)
        results.sort(key=lambda e: e.timestamp, reverse=True)
        return results

    async def delete_entry(self, project: str, entry_id: str) -> bool:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            if entry_id not in store._state.entries:
                return False
            del store._state.entries[entry_id]
            store._save_locked()
            return True

    async def get_context_summary(
        self,
        project: str,
        *,
        max_per_type: int = 5,
    ) -> str:
        """Aggregate recent entries into a formatted text block."""
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            all_entries = list(store._state.entries.values())

        if not all_entries:
            return ""

        sections: list[str] = []
        for entry_type in ("context", "decision", "review", "idea"):
            typed = [e for e in all_entries if e.type == entry_type]
            typed.sort(key=lambda e: e.timestamp, reverse=True)
            typed = typed[:max_per_type]
            if not typed:
                continue
            label = entry_type.capitalize()
            lines = [f"## {label}"]
            for e in typed:
                tag_str = f" [{', '.join(e.tags)}]" if e.tags else ""
                lines.append(f"- **{e.title}**{tag_str}: {e.content[:200]}")
            sections.append("\n".join(lines))

        return "\n\n".join(sections)
