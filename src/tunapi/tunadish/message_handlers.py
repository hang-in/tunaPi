from __future__ import annotations

from typing import Any
import anyio

from ..logging import get_logger
from .transport import TunadishTransport

logger = get_logger(__name__)


async def handle_message_retry(
    backend: Any,
    params: dict[str, Any],
    runtime: Any,
    transport: TunadishTransport,
    ws_tg: anyio.abc.TaskGroup,
) -> None:
    """message.retry -> 새 브랜치 생성 후 마지막 prompt를 재실행."""
    conv_id = params.get("conversation_id")
    message_id = params.get("message_id")
    if not conv_id or not message_id:
        return

    # 마지막 prompt 찾기
    entries = await backend._journal.recent_entries(conv_id, limit=200)
    last_prompt_text = None
    for e in reversed(entries):
        if e.event == "prompt":
            last_prompt_text = e.data.get("text", "")
            break
    if not last_prompt_text:
        return

    # 브랜치 생성 (프로젝트가 있을 때만)
    ctx = await backend.context_store.get_context(conv_id)
    project = ctx.project if ctx else None
    if project:
        meta = backend.context_store._cache.get(conv_id)
        parent_id = meta.active_branch_id if meta else None
        branch = await backend._facade.conv_branches.create(
            project,
            label=f"retry-{message_id[:6]}",
            parent_branch_id=parent_id,
            session_id=conv_id,
        )
        await backend.context_store.set_active_branch(conv_id, branch.branch_id)
        await backend._broadcast(
            "branch.created",
            {
                "conversation_id": conv_id,
                "branch_id": branch.branch_id,
                "label": branch.label,
                "parent_branch_id": parent_id,
            },
        )

    # 재실행
    ws_tg.start_soon(
        backend.handle_chat_send,
        {"conversation_id": conv_id, "text": last_prompt_text},
        runtime,
        transport,
    )


async def handle_message_save(
    backend: Any,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    """message.save -> 메시지 내용을 프로젝트 메모리에 저장."""
    conv_id = params.get("conversation_id")
    message_id = params.get("message_id")
    content = params.get("content")  # 클라이언트에서 직접 전달
    if not conv_id or not message_id:
        return

    # content가 params에 없으면 저널에서 조회
    if not content:
        entries = await backend._journal.recent_entries(conv_id, limit=200)
        for e in reversed(entries):
            if e.event == "completed" and e.data.get("ok"):
                content = e.data.get("answer", "")
                if content:
                    break
            elif e.event == "prompt":
                content = e.data.get("text", "")
                if content:
                    break

    if not content:
        await transport._send_notification(
            "message.action.result",
            {
                "action": "save",
                "ok": False,
                "error": "message not found",
            },
        )
        return

    ctx = await backend.context_store.get_context(conv_id)
    project = ctx.project if ctx else None
    if project:
        await backend._facade.memory.add_entry(
            project=project,
            type="context",
            title=f"Saved message {message_id[:8]}",
            content=content[:500],
            source="tunadish",
        )
    await transport._send_notification(
        "message.action.result",
        {
            "action": "save",
            "ok": True,
            "message_id": message_id,
        },
    )


async def handle_message_delete(
    backend: Any,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    """message.delete -> 클라이언트에 삭제 확인 알림 (저널은 append-only이므로 UI에서만 제거)."""
    conv_id = params.get("conversation_id")
    message_id = params.get("message_id")
    if not conv_id or not message_id:
        return
    # 저널은 append-only -> 클라이언트에서 UI 제거만 수행
    await transport._send_notification(
        "message.deleted",
        {
            "conversation_id": conv_id,
            "message_id": message_id,
        },
    )


async def handle_message_adopt(
    backend: Any,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    """message.adopt -> 현재 브랜치를 채택하고 메인으로 복귀. sibling 브랜치는 건드리지 않음."""
    conv_id = params.get("conversation_id")
    message_id = params.get("message_id")
    if not conv_id or not message_id:
        return

    ctx = await backend.context_store.get_context(conv_id)
    project = ctx.project if ctx else None
    meta = backend.context_store._cache.get(conv_id)
    branch_id = meta.active_branch_id if meta else None

    if project and branch_id:
        lock = backend._conv_locks.setdefault(conv_id, anyio.Lock())
        async with lock:
            await backend._facade.conv_branches.adopt(project, branch_id)
            await backend.context_store.set_active_branch(conv_id, None)
            await backend._broadcast(
                "branch.adopted",
                {
                    "conversation_id": conv_id,
                    "branch_id": branch_id,
                },
            )

    await transport._send_notification(
        "message.action.result",
        {
            "action": "adopt",
            "ok": True,
            "message_id": message_id,
        },
    )


async def handle_discussion_save(
    backend: Any,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    """discussion.save_roundtable -> DiscussionRecord 저장."""
    project = params.get("project", "")
    if not project:
        await transport._send_notification(
            "discussion.save_roundtable.result", {"error": "project required"}
        )
        return

    discussion_id = params.get("discussion_id", "")
    topic = params.get("topic", "")
    participants = params.get("participants", [])
    rounds = params.get("rounds", 0)
    transcript = params.get("transcript", [])
    summary = params.get("summary")
    branch_name = params.get("branch_name")

    record = await backend._facade.discussions.create_record(
        project,
        discussion_id=discussion_id,
        topic=topic,
        participants=participants,
        rounds=rounds,
        transcript=transcript,
        summary=summary,
        branch_name=branch_name,
    )

    if branch_name:
        await backend._facade.link_discussion_to_branch(
            project, record.discussion_id, branch_name
        )

    if params.get("auto_synthesis", False):
        await backend._facade.save_synthesis_from_discussion(
            project, record.discussion_id
        )

    await transport._send_notification(
        "discussion.save_roundtable.result",
        {
            "discussion_id": record.discussion_id,
            "project": project,
            "topic": record.topic,
            "status": record.status,
        },
    )


async def handle_discussion_link_branch(
    backend: Any,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    """discussion.link_branch -> discussion <-> branch 양방향 링크."""
    project = params.get("project", "")
    discussion_id = params.get("discussion_id", "")
    branch_name = params.get("branch_name", "")

    if not project or not discussion_id or not branch_name:
        await transport._send_notification(
            "discussion.link_branch.result",
            {"error": "project, discussion_id, branch_name required"},
        )
        return

    ok = await backend._facade.link_discussion_to_branch(
        project, discussion_id, branch_name
    )
    await transport._send_notification(
        "discussion.link_branch.result",
        {
            "ok": ok,
            "project": project,
            "discussion_id": discussion_id,
            "branch_name": branch_name,
        },
    )


async def handle_synthesis_create(
    backend: Any,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    """synthesis.create_from_discussion -> SynthesisArtifact 생성."""
    project = params.get("project", "")
    discussion_id = params.get("discussion_id", "")

    if not project or not discussion_id:
        await transport._send_notification(
            "synthesis.create.result", {"error": "project, discussion_id required"}
        )
        return

    artifact = await backend._facade.save_synthesis_from_discussion(
        project, discussion_id
    )
    if artifact is None:
        await transport._send_notification(
            "synthesis.create.result", {"error": "discussion not found"}
        )
        return

    await transport._send_notification(
        "synthesis.create.result",
        {
            "artifact_id": artifact.artifact_id,
            "project": project,
            "source_id": artifact.source_id,
            "thesis": artifact.thesis,
        },
    )


async def handle_review_request(
    backend: Any,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    """review.request -> ReviewRequest 생성."""
    project = params.get("project", "")
    artifact_id = params.get("artifact_id", "")

    if not project or not artifact_id:
        await transport._send_notification(
            "review.request.result", {"error": "project, artifact_id required"}
        )
        return

    review = await backend._facade.request_review_for_synthesis(project, artifact_id)
    if review is None:
        await transport._send_notification(
            "review.request.result", {"error": "artifact not found"}
        )
        return

    await transport._send_notification(
        "review.request.result",
        {
            "review_id": review.review_id,
            "project": project,
            "artifact_id": review.artifact_id,
            "artifact_version": review.artifact_version,
            "status": review.status,
        },
    )


async def handle_handoff_create(
    backend: Any,
    params: dict[str, Any],
    runtime: Any,
    transport: TunadishTransport,
) -> None:
    """handoff.create -> HandoffURI 생성."""
    project = params.get("project", "")
    if not project:
        await transport._send_notification(
            "handoff.create.result", {"error": "project required"}
        )
        return

    uri = await backend._facade.get_handoff_uri(
        project,
        session_id=params.get("session_id"),
        branch_id=params.get("branch_id"),
        focus=params.get("focus"),
        pending_run_id=params.get("pending_run_id"),
    )

    await transport._send_notification(
        "handoff.create.result",
        {
            "project": project,
            "uri": uri,
        },
    )


async def handle_handoff_parse(
    backend: Any,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    """handoff.parse -> HandoffURI 파싱."""
    from ..core.handoff import parse_handoff_uri

    uri_str = params.get("uri", "")
    if not uri_str:
        await transport._send_notification(
            "handoff.parse.result", {"error": "uri required"}
        )
        return

    parsed = parse_handoff_uri(uri_str)
    if parsed is None:
        await transport._send_notification(
            "handoff.parse.result", {"error": "invalid handoff URI"}
        )
        return

    await transport._send_notification(
        "handoff.parse.result",
        {
            "project": parsed.project,
            "session_id": parsed.session_id,
            "branch_id": parsed.branch_id,
            "focus": parsed.focus,
            "pending_run_id": parsed.pending_run_id,
            "engine": parsed.engine,
            "conversation_id": parsed.conversation_id,
        },
    )


async def handle_engine_list(
    backend: Any,
    runtime: Any,
    transport: TunadishTransport,
) -> None:
    """engine.list -> 사용 가능한 엔진 + 모델 목록."""
    from ..engine_models import get_models as _get_models

    engines: dict[str, list[str]] = {}
    try:
        for eid in runtime.available_engine_ids():
            models, _src = _get_models(eid)
            engines[eid] = models
    except Exception:  # noqa: BLE001, S110
        pass

    await transport._send_notification(
        "engine.list.result",
        {
            "engines": engines,
        },
    )
