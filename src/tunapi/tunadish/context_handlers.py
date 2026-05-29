from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

from ..logging import get_logger
from .transport import TunadishTransport

if TYPE_CHECKING:
    from ..core.branch_sessions import BranchRecord

    from .backend import TunadishBackend


logger = get_logger(__name__)

_RAWQ_CONTEXT_RE = re.compile(r"<relevant_code>.*?</relevant_code>\s*---\s*", re.DOTALL)
_SIBLING_CONTEXT_RE = re.compile(
    r"<sibling_sessions>.*?</sibling_sessions>\s*---\s*", re.DOTALL
)


async def handle_project_context(
    backend: TunadishBackend,
    params: dict[str, Any],
    runtime: Any,
    transport: TunadishTransport,
) -> None:
    """project.context -> ProjectContextDTO를 JSON 구조로 반환."""
    conv_id = params.get("conversation_id", "__rpc__")
    ctx = await backend.context_store.get_context(conv_id)
    project = ctx.project if ctx else params.get("project")
    if not project:
        await transport._send_notification(
            "project.context.result", {"error": "no project"}
        )
        return

    # params.project fallback으로 프로젝트가 결정된 경우, context_store에 자동 바인딩
    # (__rpc__ 같은 가상 채널은 제외)
    if (
        (ctx is None or not ctx.project)
        and conv_id != "__rpc__"
        and not conv_id.startswith("branch:")
    ):
        from ..context import RunContext as _BindRC

        await backend.context_store.set_context(conv_id, _BindRC(project=project))

    # ChatPrefs에서 엔진/모델/트리거 조회
    engine = (
        await backend._chat_prefs.get_default_engine(conv_id) or runtime.default_engine
    )
    model = (
        await backend._chat_prefs.get_engine_model(conv_id, engine) if engine else None
    )
    # ChatPrefs에 model override가 없으면 runner의 기본 model 사용
    if not model and engine:
        try:
            rr = runtime.resolve_runner(resume_token=None, engine_override=engine)
            runner_model = getattr(rr.runner, "model", None)
            if runner_model:
                model = runner_model
            else:
                # run_options에서 모델 확인
                from ..runners.run_options import get_run_options

                opts = get_run_options()
                if opts and opts.model:
                    model = opts.model
        except Exception:  # noqa: BLE001, S110
            pass
    trigger = await backend._chat_prefs.get_trigger_mode(conv_id) or "mentions"
    persona = None  # TODO: persona는 현재 global만 지원

    # facade에서 프로젝트 컨텍스트 DTO
    from ..context import RunContext as _RC

    project_path = None
    try:
        cwd = runtime.resolve_run_cwd(_RC(project=project))
        if cwd:
            project_path = str(cwd)
    except Exception as exc:  # noqa: BLE001
        logger.debug("resolve_run_cwd failed for project=%s: %s", project, exc)
    # fallback: ProjectConfig.path에서 직접 가져오기
    if not project_path:
        try:
            projects_map = getattr(getattr(runtime, "_projects", None), "projects", {})
            pc = projects_map.get(project)
            if pc and pc.path:
                project_path = str(pc.path)
        except Exception:  # noqa: BLE001, S110
            pass
    dto = await backend._facade.get_project_context_dto(
        project,
        project_path=project_path,
        default_engine=engine,
    )

    # 실제 git 현재 브랜치 조회 (비동기 — 이벤트 루프 블로킹 방지)
    git_branch = None
    logger.debug("project_context: project=%s project_path=%s", project, project_path)
    if project_path:
        try:
            import asyncio as _asyncio

            proc = await _asyncio.create_subprocess_exec(
                "git",
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
                cwd=project_path,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
            )
            stdout, stderr = await _asyncio.wait_for(proc.communicate(), timeout=3)
            logger.debug(
                "git rev-parse rc=%s stdout=%r stderr=%r",
                proc.returncode,
                stdout.decode().strip(),
                stderr.decode().strip(),
            )
            if proc.returncode == 0:
                git_branch = stdout.decode().strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("git rev-parse failed: %s", exc)

    # 사용 가능한 엔진+모델 목록
    available_engines: dict[str, list[str]] = {}
    try:
        from ..engine_models import get_models as _get_models

        for eid in runtime.available_engine_ids():
            models, _src = _get_models(eid)
            available_engines[eid] = models
    except Exception:  # noqa: BLE001, S110
        pass

    # Resume token 조회 — conv별 토큰 우선, fallback: 프로젝트 단위
    resume_token_value = None
    conv_id_for_token = params.get("conversation_id")
    if conv_id_for_token:
        conv_session = await backend._conv_sessions.get(conv_id_for_token)
        if conv_session:
            resume_token_value = conv_session.token
    if not resume_token_value:
        try:
            rt = await backend._project_sessions.get(project)
            if rt:
                resume_token_value = rt.value
        except Exception:  # noqa: BLE001, S110
            pass

    result = {
        "project": project,
        "engine": engine,
        "model": model,
        "trigger_mode": trigger,
        "persona": persona,
        "resume_token": resume_token_value,
        "git_branch": git_branch,
        "available_engines": available_engines,
        "memory_entries": [
            {
                "id": e.id,
                "type": e.type,
                "title": e.title,
                "content": e.content[:200],
                "source": e.source,
                "tags": list(e.tags),
                "timestamp": e.timestamp,
            }
            for e in dto.memory_entries
        ],
        "active_branches": [
            {
                "name": b.branch_name,
                "description": b.description or "",
                "status": b.status,
                "discussion_count": len(b.discussion_ids),
            }
            for b in dto.active_branches
        ],
        "conv_branches": [
            {
                "id": cb.branch_id,
                "label": cb.label,
                "status": cb.status,
                "git_branch": cb.git_branch,
                "parent_branch_id": cb.parent_branch_id,
                "session_id": cb.session_id,
                "checkpoint_id": cb.checkpoint_id,
            }
            for cb in (await backend._facade.conv_branches.list(project))
        ],
        "pending_review_count": len(dto.pending_reviews),
        "recent_discussions": [
            {
                "id": d.discussion_id,
                "topic": d.topic,
                "status": d.status,
                "participants": list(d.participants),
            }
            for d in dto.discussions[:5]
        ],
        "markdown": dto.markdown,
    }
    # conversation별 설정 override
    conv_s = backend.context_store.get_conv_settings(conv_id)
    conv_s_dict = conv_s.to_dict()
    if conv_s_dict:
        result["conv_settings"] = conv_s_dict
    await transport._send_notification("project.context.result", result)


async def handle_branch_list_json(
    backend: TunadishBackend,
    params: dict[str, Any],
    runtime: Any,
    transport: TunadishTransport,
) -> None:
    """branch.list.json -> Git branches + Conversation branches 구조화."""
    conv_id = params.get("conversation_id", "__rpc__")
    ctx = await backend.context_store.get_context(conv_id)
    project = ctx.project if ctx else params.get("project")
    if not project:
        await transport._send_notification(
            "branch.list.json.result", {"error": "no project"}
        )
        return

    # cast: the facade exposes branches loosely (ty infers Unknown); the call
    # is typed list[BranchRecord] at the source (branch_sessions.list_branches).
    git_branches = cast(
        "list[BranchRecord]",
        await backend._facade.branches.list_branches(project),
    )
    conv_branches = await backend._facade.conv_branches.list(project)

    result = {
        "project": project,
        "git_branches": [
            {
                "name": b.branch_name,
                "status": b.status,
                "description": b.description or "",
                "parent_branch": b.parent_branch,
                "linked_entry_count": len(b.related_entry_ids),
                "linked_discussion_count": len(b.discussion_ids),
            }
            for b in git_branches
        ],
        "conv_branches": [
            {
                "id": cb.branch_id,
                "label": cb.label,
                "status": cb.status,
                "git_branch": cb.git_branch,
                "parent_branch_id": cb.parent_branch_id,
                "session_id": cb.session_id,
                "checkpoint_id": cb.checkpoint_id,
            }
            for cb in conv_branches
        ],
    }
    await transport._send_notification("branch.list.json.result", result)


async def handle_memory_list_json(
    backend: TunadishBackend,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    """memory.list.json -> MemoryEntry[] 구조화."""
    conv_id = params.get("conversation_id", "__rpc__")
    ctx = await backend.context_store.get_context(conv_id)
    project = ctx.project if ctx else params.get("project")
    if not project:
        await transport._send_notification(
            "memory.list.json.result", {"error": "no project"}
        )
        return

    entry_type = params.get("type")
    limit = params.get("limit", 50)
    entries = await backend._facade.memory.list_entries(
        project, type=entry_type, limit=limit
    )

    result = {
        "project": project,
        "entries": [
            {
                "id": e.id,
                "type": e.type,
                "title": e.title,
                "content": e.content,
                "source": e.source,
                "tags": list(e.tags),
                "timestamp": e.timestamp,
            }
            for e in entries
        ],
    }
    await transport._send_notification("memory.list.json.result", result)


async def handle_review_list_json(
    backend: TunadishBackend,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    """review.list.json -> ReviewEntry[] 구조화."""
    conv_id = params.get("conversation_id", "__rpc__")
    ctx = await backend.context_store.get_context(conv_id)
    project = ctx.project if ctx else params.get("project")
    if not project:
        await transport._send_notification(
            "review.list.json.result", {"error": "no project"}
        )
        return

    status = params.get("status")
    reviews = await backend._facade.reviews.list(project, status=status)

    result = {
        "project": project,
        "reviews": [
            {
                "id": r.review_id,
                "artifact_id": r.artifact_id,
                "artifact_version": r.artifact_version,
                "status": r.status,
                "reviewer_comment": r.reviewer_comment or "",
                "created_at": r.created_at,
            }
            for r in reviews
        ],
    }
    await transport._send_notification("review.list.json.result", result)


async def handle_project_list(
    backend: TunadishBackend,
    params: dict[str, Any],
    runtime: Any,
    transport: TunadishTransport,
) -> None:
    configured_aliases = list(runtime.project_aliases())
    discovered = backend._discover_projects(configured_aliases)
    configured = []
    projects_map = getattr(getattr(runtime, "_projects", None), "projects", {})
    for key, pc in projects_map.items():
        p_path = pc.path if pc.path else None
        is_channel = bool(
            getattr(pc, "chat_id", None) and p_path and not (p_path / ".git").is_dir()
        )
        configured.append(
            {
                "key": key,
                "alias": pc.alias,
                "path": str(p_path) if p_path else None,
                "default_engine": pc.default_engine,
                "type": "channel" if is_channel else "project",
            }
        )
    known_keys = {c["key"] for c in configured}
    configured.extend(
        {
            "key": alias.lower(),
            "alias": alias,
            "path": None,
            "default_engine": None,
        }
        for alias in configured_aliases
        if alias.lower() not in known_keys
    )
    await transport._send_notification(
        "project.list.result",
        {
            "configured": configured,
            "discovered": discovered,
        },
    )


async def handle_conversation_create(
    backend: TunadishBackend,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    conv_id = params.get("conversation_id")
    project = params.get("project")
    label = params.get("label")
    if conv_id and project:
        from ..context import RunContext

        await backend.context_store.set_context(
            conv_id,
            RunContext(project=project),
            label=label,
        )
        await transport._send_notification(
            "conversation.created",
            {
                "conversation_id": conv_id,
                "project": project,
                "label": label or conv_id[:8],
            },
        )


async def handle_conversation_delete(
    backend: TunadishBackend,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    conv_id = params.get("conversation_id")
    if conv_id:
        await backend.context_store.clear(conv_id)
        journal_path = backend._journal._base_dir / f"{conv_id}.jsonl"
        if journal_path.exists():
            journal_path.unlink()
        await transport._send_notification(
            "conversation.deleted",
            {
                "conversation_id": conv_id,
            },
        )


async def handle_conversation_list(
    backend: TunadishBackend,
    params: dict[str, Any],
    runtime: Any,
    transport: TunadishTransport,
) -> None:
    project_filter = params.get("project")
    convs = backend.context_store.list_conversations(project=project_filter)
    for c in convs:
        c["source"] = "tunadish"
    if project_filter:
        try:
            chat_ids = runtime.chat_ids_for_project(project_filter)
        except Exception:  # noqa: BLE001
            chat_ids = []
        for transport_name, journal in backend._cross_journals:
            for cid in chat_ids:
                try:
                    cid_str = str(cid)
                    journal_path = journal._base_dir / f"{cid_str}.jsonl"
                    if journal_path.exists():
                        entries = await journal.recent_entries(cid_str, limit=1)
                        last_ts = entries[-1].timestamp if entries else ""
                        convs.append(
                            {
                                "id": cid_str,
                                "project": project_filter,
                                "branch": None,
                                "label": f"{transport_name}",
                                "created_at": 0.0,
                                "source": transport_name,
                                "last_activity": last_ts,
                            }
                        )
                except Exception as cross_err:  # noqa: BLE001
                    logger.debug(
                        "Cross-transport journal lookup failed for %s/%s: %s",
                        transport_name,
                        cid,
                        cross_err,
                    )
    await transport._send_notification(
        "conversation.list.result",
        {
            "conversations": convs,
        },
    )


async def handle_conversation_history(
    backend: TunadishBackend,
    params: dict[str, Any],
    transport: TunadishTransport,
) -> None:
    conv_id = params.get("conversation_id")
    branch_id = params.get("branch_id")
    if conv_id:
        history_channel = f"branch:{branch_id}" if branch_id else conv_id
        all_entries = []
        td_entries = await backend._journal.recent_entries(history_channel, limit=200)
        if td_entries:
            all_entries.extend(td_entries)
        if not branch_id:
            for tname, j in backend._cross_journals:
                try:
                    cross_entries = await j.recent_entries(conv_id, limit=200)
                    if cross_entries:
                        all_entries.extend(cross_entries)
                except Exception as cross_err:  # noqa: BLE001
                    logger.debug(
                        "Cross-transport history failed for %s/%s: %s",
                        tname,
                        conv_id,
                        cross_err,
                    )
        entries = sorted(all_entries, key=lambda e: e.timestamp)
        messages = []
        run_meta: dict[str, dict[str, str | None]] = {}
        for e in entries:
            if e.event == "prompt":
                raw_text = e.data.get("text", "")
                clean_text = _RAWQ_CONTEXT_RE.sub("", raw_text)
                clean_text = _SIBLING_CONTEXT_RE.sub("", clean_text)
                meta = {"engine": e.engine, "model": e.data.get("model")}
                run_meta[e.run_id] = meta
                messages.append(
                    {
                        "role": "user",
                        "content": clean_text,
                        "timestamp": e.timestamp,
                    }
                )
            elif e.event == "completed" and e.data.get("ok"):
                answer = e.data.get("answer")
                if answer:
                    meta = run_meta.get(e.run_id, {})
                    msg: dict[str, Any] = {
                        "role": "assistant",
                        "content": answer,
                        "timestamp": e.timestamp,
                    }
                    if meta.get("engine"):
                        msg["engine"] = meta["engine"]
                    if meta.get("model"):
                        msg["model"] = meta["model"]
                    messages.append(msg)
        await transport._send_notification(
            "conversation.history.result",
            {
                "conversation_id": history_channel,
                "messages": messages,
            },
        )
