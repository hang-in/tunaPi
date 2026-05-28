"""Review requests — approve/reject artifacts with comments.

Each project gets its own JSON file at
``~/.tunapi/project_memory/{alias}_reviews.json``.
"""

from __future__ import annotations

import time
from builtins import list as list_type
from pathlib import Path
from typing import Literal, cast

import msgspec

from ..logging import get_logger
from ..state_store import JsonStateStore
from .project_memory import generate_entry_id

logger = get_logger(__name__)

ReviewStatus = Literal["pending", "approved", "rejected"]

_STATE_VERSION = 1


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class ReviewRequest(msgspec.Struct, forbid_unknown_fields=False):
    review_id: str
    artifact_id: str
    artifact_version: int
    status: ReviewStatus = "pending"
    reviewer_comment: str = ""
    created_at: str = ""
    resolved_at: str | None = None


class _State(msgspec.Struct, forbid_unknown_fields=False):
    version: int = _STATE_VERSION
    reviews: dict[str, ReviewRequest] = msgspec.field(default_factory=dict)


class ReviewStore:
    """Per-project review request store."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._stores: dict[str, JsonStateStore[_State]] = {}

    def _store_for(self, project: str) -> JsonStateStore[_State]:
        store = self._stores.get(project)
        if store is None:
            path = self._base_dir / f"{project}_reviews.json"
            store = cast(
                JsonStateStore[_State],
                JsonStateStore(
                    path,
                    version=_STATE_VERSION,
                    state_type=_State,
                    state_factory=_State,
                    log_prefix="review",
                    logger=logger,
                ),
            )
            self._stores[project] = store
        return store

    async def request_review(
        self,
        project: str,
        *,
        artifact_id: str,
        artifact_version: int = 1,
    ) -> ReviewRequest:
        review = ReviewRequest(
            review_id=generate_entry_id(),
            artifact_id=artifact_id,
            artifact_version=artifact_version,
            created_at=_now_iso(),
        )
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            store._state.reviews[review.review_id] = review
            store._save_locked()
        return review

    async def get(self, project: str, review_id: str) -> ReviewRequest | None:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            return store._state.reviews.get(review_id)

    async def list(
        self,
        project: str,
        *,
        status: ReviewStatus | None = None,
    ) -> list_type[ReviewRequest]:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            reviews = list(store._state.reviews.values())
        if status is not None:
            reviews = [r for r in reviews if r.status == status]
        reviews.sort(key=lambda r: r.created_at, reverse=True)
        return reviews

    async def approve(
        self,
        project: str,
        review_id: str,
        *,
        comment: str = "",
    ) -> ReviewRequest | None:
        return await self._resolve(project, review_id, "approved", comment)

    async def reject(
        self,
        project: str,
        review_id: str,
        *,
        comment: str = "",
    ) -> ReviewRequest | None:
        return await self._resolve(project, review_id, "rejected", comment)

    async def _resolve(
        self,
        project: str,
        review_id: str,
        target: ReviewStatus,
        comment: str,
    ) -> ReviewRequest | None:
        store = self._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            review = store._state.reviews.get(review_id)
            if review is None:
                return None
            review.status = target
            review.resolved_at = _now_iso()
            if comment:
                review.reviewer_comment = comment
            store._save_locked()
            return review
