"""Branch lifecycle tracking — create, merge, abandon per project.

Each project gets its own JSON file at
``~/.tunapi/project_memory/{alias}_branches.json``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal, cast

import msgspec

from ..logging import get_logger
from ..state_store import JsonStateStore

logger = get_logger(__name__)

BranchStatus = Literal["active", "merged", "abandoned"]

_STATE_VERSION = 1


class BranchRecord(msgspec.Struct, forbid_unknown_fields=False):
    branch_name: str
    project_alias: str
    status: BranchStatus = "active"
    created_at: float = 0.0
    updated_at: float = 0.0
    parent_branch: str | None = None
    description: str = ""
    related_entry_ids: list[str] = msgspec.field(default_factory=list)
    discussion_ids: list[str] = msgspec.field(default_factory=list)


class _State(msgspec.Struct, forbid_unknown_fields=False):
    version: int = _STATE_VERSION
    branches: dict[str, BranchRecord] = msgspec.field(default_factory=dict)


class BranchSessionStore:
    """Per-project branch lifecycle store."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._stores: dict[str, JsonStateStore[_State]] = {}

    def _store_for(self, project: str) -> JsonStateStore[_State]:
        store = self._stores.get(project)
        if store is None:
            path = self._base_dir / f"{project}_branches.json"
            store = cast(
                JsonStateStore[_State],
                JsonStateStore(
                    path,
                    version=_STATE_VERSION,
                    state_type=_State,
                    state_factory=_State,
                    log_prefix="branch_sessions",
                    logger=logger,
                ),
            )
            self._stores[project] = store
        return store

    async def create_branch(
        self,
        project: str,
        branch_name: str,
        *,
        parent_branch: str | None = None,
        description: str = "",
    ) -> BranchRecord:
        now = time.time()
        record = BranchRecord(
            branch_name=branch_name,
            project_alias=project,
            status="active",
            created_at=now,
            updated_at=now,
            parent_branch=parent_branch,
            description=description,
        )
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            store._state.branches[branch_name] = record
            store._save_locked()
        return record

    async def get_branch(self, project: str, branch_name: str) -> BranchRecord | None:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            return store._state.branches.get(branch_name)

    async def list_branches(
        self,
        project: str,
        *,
        status: BranchStatus | None = None,
    ) -> list[BranchRecord]:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            branches = list(store._state.branches.values())
        if status is not None:
            branches = [b for b in branches if b.status == status]
        branches.sort(key=lambda b: b.updated_at, reverse=True)
        return branches

    async def merge_branch(self, project: str, branch_name: str) -> BranchRecord | None:
        return await self._transition(project, branch_name, "merged")

    async def abandon_branch(
        self, project: str, branch_name: str
    ) -> BranchRecord | None:
        return await self._transition(project, branch_name, "abandoned")

    async def link_entry(self, project: str, branch_name: str, entry_id: str) -> bool:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            record = store._state.branches.get(branch_name)
            if record is None:
                return False
            if entry_id not in record.related_entry_ids:
                record.related_entry_ids.append(entry_id)
                record.updated_at = time.time()
                store._save_locked()
        return record is not None

    async def _transition(
        self, project: str, branch_name: str, target: BranchStatus
    ) -> BranchRecord | None:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            record = store._state.branches.get(branch_name)
            if record is None:
                return None
            record.status = target
            record.updated_at = time.time()
            store._save_locked()
            return record
