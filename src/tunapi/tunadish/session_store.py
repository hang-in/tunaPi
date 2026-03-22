"""tunadish conversation별 독립 resume token 저장소."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import anyio

from ..logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class SessionEntry:
    engine: str
    token: str
    cwd: str | None = None


class ConversationSessionStore:
    """conversation_id → resume token 매핑.

    tunapi의 ProjectSessionStore(프로젝트 단위)와 별도로,
    tunadish 세션별 독립 토큰을 관리한다.
    """

    def __init__(self, storage_path: Path) -> None:
        self._path = storage_path
        self._lock = anyio.Lock()
        self._cache: dict[str, SessionEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text("utf-8"))
            for conv_id, entry in data.get("conversations", {}).items():
                self._cache[conv_id] = SessionEntry(
                    engine=entry["engine"],
                    token=entry["token"],
                    cwd=entry.get("cwd"),
                )
        except Exception:
            logger.warning("conv_session_store.load_failed", path=str(self._path))

    async def _save(self) -> None:
        async with self._lock:
            data = {
                "version": 1,
                "conversations": {
                    cid: {"engine": e.engine, "token": e.token, "cwd": e.cwd}
                    for cid, e in self._cache.items()
                },
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), "utf-8"
            )

    async def get(self, conv_id: str) -> SessionEntry | None:
        return self._cache.get(conv_id)

    async def set(
        self, conv_id: str, engine: str, token: str, cwd: str | None = None
    ) -> None:
        self._cache[conv_id] = SessionEntry(engine=engine, token=token, cwd=cwd)
        await self._save()

    async def clear(self, conv_id: str) -> None:
        self._cache.pop(conv_id, None)
        await self._save()
