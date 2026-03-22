"""Conversation branch — dialogue-level branching within a project.

Unlike :class:`BranchRecord` in ``branch_sessions.py`` (keyed by git
branch name), a conversation branch represents a **dialogue fork** —
an experiment, review, or alternative discussion that may or may not
map to a git branch.

Each project gets its own JSON file at
``~/.tunapi/project_memory/{alias}_conv_branches.json``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

import msgspec

from ..logging import get_logger
from ..state_store import JsonStateStore
from .project_memory import generate_entry_id

logger = get_logger(__name__)

ConvBranchStatus = Literal["active", "adopted", "archived", "discarded"]

_STATE_VERSION = 1


class ConversationBranch(msgspec.Struct, forbid_unknown_fields=False):
    branch_id: str
    label: str
    status: ConvBranchStatus = "active"
    parent_branch_id: str | None = None
    session_id: str | None = None  # chat_sessions key linkage
    git_branch: str | None = None  # optional git branch linkage
    checkpoint_id: str | None = None  # 분기 시점의 메시지/utterance ID
    rt_session_id: str | None = None  # RT 세션 연결 (None이면 일반 브랜치)
    created_at: str = ""
    updated_at: str = ""


class _State(msgspec.Struct, forbid_unknown_fields=False):
    version: int = _STATE_VERSION
    branches: dict[str, ConversationBranch] = msgspec.field(default_factory=dict)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class ConversationBranchStore:
    """Per-project conversation branch store."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._stores: dict[str, JsonStateStore[_State]] = {}

    def _store_for(self, project: str) -> JsonStateStore[_State]:
        store = self._stores.get(project)
        if store is None:
            path = self._base_dir / f"{project}_conv_branches.json"
            store = JsonStateStore(
                path,
                version=_STATE_VERSION,
                state_type=_State,
                state_factory=_State,
                log_prefix="conversation_branch",
                logger=logger,
            )
            self._stores[project] = store
            # Migrate legacy "merged" → "adopted"
            _migrate_merged_to_adopted(store)
        return store

    async def create(
        self,
        project: str,
        label: str,
        *,
        parent_branch_id: str | None = None,
        session_id: str | None = None,
        git_branch: str | None = None,
        checkpoint_id: str | None = None,
    ) -> ConversationBranch:
        now = _now_iso()
        branch = ConversationBranch(
            branch_id=generate_entry_id(),
            label=label,
            parent_branch_id=parent_branch_id,
            session_id=session_id,
            git_branch=git_branch,
            checkpoint_id=checkpoint_id,
            created_at=now,
            updated_at=now,
        )
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            store._state.branches[branch.branch_id] = branch
            store._save_locked()
        return branch

    async def get(self, project: str, branch_id: str) -> ConversationBranch | None:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            return store._state.branches.get(branch_id)

    async def list(
        self,
        project: str,
        *,
        status: ConvBranchStatus | None = None,
    ) -> list[ConversationBranch]:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            branches = list(store._state.branches.values())
        if status is not None:
            branches = [b for b in branches if b.status == status]
        branches.sort(key=lambda b: b.updated_at, reverse=True)
        return branches

    async def adopt(self, project: str, branch_id: str) -> ConversationBranch | None:
        return await self._transition(project, branch_id, "adopted")

    async def archive(self, project: str, branch_id: str) -> ConversationBranch | None:
        return await self._transition(project, branch_id, "archived")

    async def discard(self, project: str, branch_id: str) -> ConversationBranch | None:
        return await self._transition(project, branch_id, "discarded")

    async def remove(self, project: str, branch_id: str) -> ConversationBranch | None:
        """Permanently delete a branch record from the store."""
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            branch = store._state.branches.pop(branch_id, None)
            if branch is not None:
                store._save_locked()
            return branch

    # Backward compat alias
    async def merge(self, project: str, branch_id: str) -> ConversationBranch | None:
        return await self.adopt(project, branch_id)

    async def link_session(self, project: str, branch_id: str, session_id: str) -> bool:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            branch = store._state.branches.get(branch_id)
            if branch is None:
                return False
            branch.session_id = session_id
            branch.updated_at = _now_iso()
            store._save_locked()
            return True

    async def link_git_branch(
        self, project: str, branch_id: str, git_branch: str
    ) -> bool:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            branch = store._state.branches.get(branch_id)
            if branch is None:
                return False
            branch.git_branch = git_branch
            branch.updated_at = _now_iso()
            store._save_locked()
            return True

    async def _transition(
        self, project: str, branch_id: str, target: ConvBranchStatus
    ) -> ConversationBranch | None:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            branch = store._state.branches.get(branch_id)
            if branch is None:
                return None
            branch.status = target
            branch.updated_at = _now_iso()
            store._save_locked()
            return branch


def _migrate_merged_to_adopted(store: JsonStateStore[_State]) -> None:
    """Migrate legacy 'merged' status to 'adopted' on first load."""
    if not store._path.exists():
        return
    try:
        raw = store._path.read_bytes()
    except Exception:  # noqa: BLE001
        return
    if b'"merged"' not in raw:
        return
    raw = raw.replace(b'"merged"', b'"adopted"')
    store._path.write_bytes(raw)
    logger.info("conversation_branch.migrated_merged_to_adopted", path=str(store._path))
