import json
import anyio
import time
from dataclasses import dataclass
from pathlib import Path

from ..context import RunContext


@dataclass
class ConversationMeta:
    project: str
    branch: str | None
    label: str
    created_at: float  # unix timestamp
    active_branch_id: str | None = None  # 현재 활성 대화 브랜치


class ConversationContextStore:
    """
    tunadish 클라이언트의 각 대화(conversation_id)에 연결된
    환경 컨텍스트(project, branch 등)를 관리합니다.
    """

    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self._lock = anyio.Lock()
        self._cache: dict[str, ConversationMeta] = {}
        self._load()

    def _load(self) -> None:
        if not self.storage_path.exists():
            return
        try:
            data = json.loads(self.storage_path.read_text("utf-8"))
            for conv_id, ctx_data in data.get("conversations", {}).items():
                self._cache[conv_id] = ConversationMeta(
                    project=ctx_data.get("project", ""),
                    branch=ctx_data.get("branch"),
                    label=ctx_data.get("label", conv_id[:8]),
                    created_at=ctx_data.get("created_at", 0.0),
                    active_branch_id=ctx_data.get("active_branch_id"),
                )
        except Exception:
            pass

    async def _save(self) -> None:
        async with self._lock:
            data = {
                "conversations": {
                    conv_id: {
                        "project": m.project,
                        "branch": m.branch,
                        "label": m.label,
                        "created_at": m.created_at,
                        "active_branch_id": m.active_branch_id,
                    }
                    for conv_id, m in self._cache.items()
                }
            }
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            self.storage_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), "utf-8"
            )

    async def get_context(self, conv_id: str) -> RunContext | None:
        m = self._cache.get(conv_id)
        if m is None:
            return None
        return RunContext(project=m.project, branch=m.branch)

    async def set_context(
        self,
        conv_id: str,
        context: RunContext,
        *,
        label: str | None = None,
    ) -> None:
        existing = self._cache.get(conv_id)
        self._cache[conv_id] = ConversationMeta(
            project=context.project,
            branch=context.branch,
            label=label if label is not None else (existing.label if existing else conv_id[:8]),
            created_at=existing.created_at if existing else time.time(),
        )
        await self._save()

    def list_conversations(self, project: str | None = None) -> list[dict]:
        """저장된 대화 목록 반환. project 지정 시 해당 프로젝트만 필터."""
        result = [
            {
                "id": conv_id,
                "project": m.project,
                "branch": m.branch,
                "label": m.label,
                "created_at": m.created_at,
            }
            for conv_id, m in self._cache.items()
            if (project is None or m.project == project)
            and conv_id != "__rpc__"  # 가상 채널 제외
        ]
        return sorted(result, key=lambda x: x["created_at"], reverse=True)

    async def set_active_branch(self, conv_id: str, branch_id: str | None) -> None:
        """활성 대화 브랜치 설정. None이면 메인으로 복귀."""
        meta = self._cache.get(conv_id)
        if meta:
            meta.active_branch_id = branch_id
            await self._save()

    async def clear(self, conv_id: str) -> None:
        self._cache.pop(conv_id, None)
        await self._save()
