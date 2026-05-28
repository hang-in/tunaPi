from __future__ import annotations

import uuid
import time as _time
from typing import Any
import anyio

from ..logging import get_logger
from .transport import TunadishTransport

logger = get_logger(__name__)


async def handle_branch_create(
    backend: Any,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    """branch.create -> 대화 브랜치 생성."""
    conv_id = params.get("conversation_id")
    label = params.get("label", "")
    checkpoint_id = params.get("checkpoint_id")
    if not conv_id:
        return

    # branch: 채널이면 parent conv_id로 resolve
    if conv_id.startswith("branch:"):
        conv_id = await backend._resolve_context_conv_id(conv_id)

    ctx = await backend.context_store.get_context(conv_id)
    project = ctx.project if ctx else None
    if not project:
        return

    # 클라이언트가 parent_branch_id를 명시하면 우선, 아니면 active_branch_id 폴백
    if "parent_branch_id" in params:
        parent_id = params["parent_branch_id"]  # null 명시 -> 루트 브랜치
    else:
        meta = backend.context_store._cache.get(conv_id)
        parent_id = getattr(meta, "active_branch_id", None) if meta else None

    # 라벨 자동 생성: 기존 브랜치(모든 상태 포함) 수 기반 카운터 — 이름 충돌 방지
    if not label:
        all_branches = await backend._facade.conv_branches.list(project)
        # 기존 branch-N 패턴에서 최대 N 추출
        max_n = 0
        for b in all_branches:
            if b.label.startswith("branch-"):
                try:
                    n = int(b.label.split("-", 1)[1])
                    max_n = max(max_n, n)
                except (ValueError, IndexError):
                    pass
        label = f"branch-{max_n + 1}"

    branch = await backend._facade.conv_branches.create(
        project,
        label=label,
        parent_branch_id=parent_id,
        session_id=conv_id,
        checkpoint_id=checkpoint_id,
    )

    # active_branch_id 갱신
    await backend.context_store.set_active_branch(conv_id, branch.branch_id)

    # 브랜치 전용 채널 설정: 부모 conv의 context 복사 + settings 상속
    branch_channel = f"branch:{branch.branch_id}"
    from ..context import RunContext as _BranchRC

    await backend.context_store.set_context(
        branch_channel, _BranchRC(project=project), label=label
    )
    await backend.context_store.copy_conv_settings(conv_id, branch_channel)

    # 브랜치 전용 채널에 분기점 컨텍스트를 journal에 저장
    context_summary = await build_branch_context(backend, conv_id, checkpoint_id)
    if context_summary:
        ctx_run_id = str(uuid.uuid4())
        ts = _time.strftime("%Y-%m-%dT%H:%M:%S")
        from ..journal import JournalEntry

        await backend._journal.append(
            JournalEntry(
                run_id=ctx_run_id,
                channel_id=branch_channel,
                timestamp=ts,
                event="completed",
                data={"ok": True, "answer": context_summary},
            )
        )
        # 실시간 알림 (이미 열려 있는 창에)
        ctx_msg_id = str(uuid.uuid4())
        await backend._broadcast(
            "message.new",
            {
                "ref": {"channel_id": branch_channel, "message_id": ctx_msg_id},
                "message": {"text": context_summary},
            },
        )

    await backend._broadcast(
        "branch.created",
        {
            "conversation_id": conv_id,
            "branch_id": branch.branch_id,
            "label": branch.label,
            "parent_branch_id": parent_id,
        },
    )


async def handle_branch_switch(
    backend: Any,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    """branch.switch -> 브랜치 전환."""
    conv_id = params.get("conversation_id")
    branch_id = params.get("branch_id")  # None이면 메인으로 복귀
    if not conv_id:
        return

    await backend.context_store.set_active_branch(conv_id, branch_id)

    await transport._send_notification(
        "branch.switched",
        {
            "conversation_id": conv_id,
            "branch_id": branch_id,
        },
    )


async def handle_branch_adopt(
    backend: Any,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    """branch.adopt -> 브랜치 채택, 요약 카드 삽입. sibling 브랜치는 건드리지 않음."""
    conv_id = params.get("conversation_id")
    branch_id = params.get("branch_id")
    if not conv_id or not branch_id:
        return

    ctx = await backend.context_store.get_context(conv_id)
    project = ctx.project if ctx else None
    if not project:
        return

    # per-conv 락으로 race condition 방지
    lock = backend._conv_locks.setdefault(conv_id, anyio.Lock())
    async with lock:
        # 채택 대상 브랜치 조회
        target = await backend._facade.conv_branches.get(project, branch_id)
        if not target:
            return

        # conv_id 검증: 브랜치의 session_id와 불일치 시 보정
        effective_conv_id = conv_id
        if (
            hasattr(target, "session_id")
            and target.session_id
            and target.session_id != conv_id
        ):
            logger.warning(
                "branch.adopt.conv_id_mismatch",
                requested=conv_id,
                actual=target.session_id,
                branch_id=branch_id,
            )
            effective_conv_id = target.session_id

        # 요약 카드용: 브랜치 대화에서 마지막 assistant 응답 발췌
        summary_text = await build_adopt_summary(backend, target, effective_conv_id)

        # 채택
        await backend._facade.conv_branches.adopt(project, branch_id)

        # 메인으로 복귀
        await backend.context_store.set_active_branch(effective_conv_id, None)

        # 요약 카드를 메인 타임라인에 삽입
        if summary_text:
            summary_msg_id = str(uuid.uuid4())
            await transport._send_notification(
                "message.new",
                {
                    "ref": {
                        "channel_id": effective_conv_id,
                        "message_id": summary_msg_id,
                    },
                    "message": {"text": summary_text},
                },
            )
            await transport._send_notification(
                "message.update",
                {
                    "ref": {
                        "channel_id": effective_conv_id,
                        "message_id": summary_msg_id,
                    },
                    "message": {"text": summary_text},
                },
            )

        await backend._broadcast(
            "branch.adopted",
            {
                "conversation_id": effective_conv_id,
                "branch_id": branch_id,
            },
        )


async def build_adopt_summary(backend: Any, branch: Any, conv_id: str) -> str:
    """브랜치 대화에서 요약 텍스트를 생성 (마지막 assistant 응답 발췌)."""
    label = branch.label or branch.branch_id[:8]
    try:
        entries = await backend._journal.recent_entries(conv_id, limit=200)
    except Exception:  # noqa: BLE001
        entries = []

    last_response = ""
    turn_count = 0
    for e in reversed(entries):
        if hasattr(e, "event"):
            if e.event == "prompt":
                turn_count += 1
            if e.event == "response" and not last_response:
                last_response = (e.data.get("text", "") or "")[:200]

    excerpt = (
        f"> {last_response}{'...' if len(last_response) >= 200 else ''}\n\n"
        if last_response
        else ""
    )
    turn_info = (
        f"*{turn_count}턴 대화 · {branch.branch_id[:8]}*"
        if turn_count > 0
        else f"*{branch.branch_id[:8]}*"
    )

    return f"<!-- branch-adopt-summary -->\n🔀 **브랜치 '{label}' 채택됨**\n\n{excerpt}{turn_info}"


async def build_branch_context(
    backend: Any, conv_id: str, checkpoint_id: str | None
) -> str:
    """분기점까지의 대화 요약을 브랜치 컨텍스트로 생성."""
    try:
        entries = await backend._journal.recent_entries(conv_id, limit=200)
    except Exception:  # noqa: BLE001
        entries = []
    if not entries:
        return ""

    # checkpoint_id가 있으면 해당 메시지까지만, 없으면 마지막 대화까지
    lines: list[str] = []
    for e in entries:
        if e.event == "prompt":
            text = (e.data.get("text", "") or "")[:300]
            lines.append(f"**User:** {text}")
        elif e.event == "completed" and e.data.get("ok"):
            answer = (e.data.get("answer", "") or "")[:300]
            if answer:
                lines.append(f"**Assistant:** {answer}")
        # checkpoint 도달 시 중단
        if checkpoint_id and hasattr(e, "run_id") and e.run_id == checkpoint_id:
            break

    if not lines:
        return ""

    # 마지막 4개 턴만 표시 (너무 길지 않게)
    visible = lines[-8:] if len(lines) > 8 else lines
    if len(lines) > 8:
        visible = [f"*...{len(lines) - 8}개 이전 메시지 생략...*", ""] + visible

    return "<!-- branch-context -->\n" + "\n\n".join(visible)


async def handle_branch_archive(
    backend: Any,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    """branch.archive -> 브랜치 보관."""
    conv_id = params.get("conversation_id")
    branch_id = params.get("branch_id")
    if not conv_id or not branch_id:
        return

    ctx = await backend.context_store.get_context(conv_id)
    project = ctx.project if ctx else None
    if not project:
        return

    await backend._facade.conv_branches.archive(project, branch_id)

    # 현재 보고 있던 브랜치가 archived되면 메인으로 복귀
    meta = backend.context_store._cache.get(conv_id)
    if meta and getattr(meta, "active_branch_id", None) == branch_id:
        await backend.context_store.set_active_branch(conv_id, None)

    await backend._broadcast(
        "branch.archived",
        {
            "conversation_id": conv_id,
            "branch_id": branch_id,
        },
    )


async def handle_branch_delete(
    backend: Any,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    """branch.delete -> 브랜치 영구 삭제 (remove)."""
    conv_id = params.get("conversation_id")
    branch_id = params.get("branch_id")
    if not conv_id or not branch_id:
        return

    # branch: 채널이 들어올 경우 parent conv_id로 resolve
    if conv_id.startswith("branch:"):
        conv_id = await backend._resolve_context_conv_id(conv_id)

    ctx = await backend.context_store.get_context(conv_id)
    project = ctx.project if ctx else None
    if not project:
        logger.warning("branch.delete: no project context for conv_id=%s", conv_id)
        return

    # 브랜치 기록 영구 삭제
    await backend._facade.conv_branches.remove(project, branch_id)

    # 브랜치 전용 채널의 journal 엔트리 정리
    branch_channel = f"branch:{branch_id}"
    import contextlib

    with contextlib.suppress(AttributeError, Exception):
        await backend._journal.clear_channel(branch_channel)

    # 현재 보고 있던 브랜치가 삭제되면 메인으로 복귀
    meta = backend.context_store._cache.get(conv_id)
    if meta and getattr(meta, "active_branch_id", None) == branch_id:
        await backend.context_store.set_active_branch(conv_id, None)

    # 모든 윈도우에 알림 (메인 + 브랜치 창)
    await backend._broadcast(
        "branch.deleted",
        {
            "conversation_id": conv_id,
            "branch_id": branch_id,
        },
    )
