from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..logging import get_logger

if TYPE_CHECKING:
    from ..transport_runtime import TransportRuntime

    from .backend import TunadishBackend

    from .transport import TunadishTransport

logger = get_logger(__name__)


async def rawq_startup_check(backend: TunadishBackend) -> None:
    """Check rawq availability and notify connected clients about updates."""
    from . import rawq_bridge

    if not rawq_bridge.is_available():
        logger.info("rawq: not installed (code search disabled)")
        return

    version = await rawq_bridge.get_version()
    logger.info("rawq %s available", version or "unknown")

    update_info = await rawq_bridge.check_for_update()
    if update_info and update_info.get("has_update"):
        commits = update_info.get("commits", [])
        msg = (
            f"rawq 업데이트 가능: {update_info['current']} → {update_info['latest']}"
            f" ({len(commits)}개 새 커밋)"
        )
        logger.info(msg)
        await backend._broadcast(
            "command.result",
            {
                "command": "rawq",
                "conversation_id": "__system__",
                "text": f"🔄 {msg}\n`./scripts/update-rawq.sh --apply`로 업데이트하세요.",
            },
        )


async def rawq_ensure_index(
    backend: TunadishBackend,
    project_name: str,
    runtime: TransportRuntime,
    transport: TunadishTransport,
) -> None:
    """Ensure a rawq index exists for the project."""
    from . import rawq_bridge

    if not rawq_bridge.is_available():
        return

    project_path = backend._resolve_project_path(project_name, runtime)
    if not project_path:
        return

    status = await rawq_bridge.check_index(project_path)
    if status is not None:
        logger.debug("rawq index exists for %s", project_name)
        return

    logger.info("Building rawq index for %s at %s", project_name, project_path)
    await transport._send_notification(
        "command.result",
        {
            "command": "rawq",
            "conversation_id": "__system__",
            "text": f"🔍 프로젝트 `{project_name}` 코드 인덱스를 생성합니다...",
        },
    )

    ok = await rawq_bridge.build_index(project_path)

    msg = (
        f"✅ `{project_name}` 인덱스 생성 완료."
        if ok
        else f"⚠️ `{project_name}` 인덱스 생성 실패. rawq 없이 계속합니다."
    )
    await transport._send_notification(
        "command.result",
        {
            "command": "rawq",
            "conversation_id": "__system__",
            "text": msg,
        },
    )


async def rawq_enrich_message(
    backend: TunadishBackend,
    text: str,
    project_name: str,
    runtime: TransportRuntime,
) -> str:
    """Attach rawq search results to a message."""
    from . import rawq_bridge

    if not rawq_bridge.is_available():
        return text

    project_path = backend._resolve_project_path(project_name, runtime)
    if not project_path:
        return text

    text_len = len(text)
    if text_len < 100:
        token_budget = 4000
    elif text_len < 500:
        token_budget = 2000
    else:
        token_budget = 1000

    result = await rawq_bridge.search(
        query=text,
        project_path=project_path,
        top=5,
        token_budget=token_budget,
        threshold=0.5,
    )

    context_block = rawq_bridge.format_context_block(result) if result else ""

    if context_block:
        result_count = len(result.get("results", [])) if result is not None else 0
        logger.info(
            "rawq.enrich",
            project=project_name,
            results=result_count,
            token_budget=token_budget,
        )
        return f"{context_block}\n\n---\n\n{text}"

    map_result = await rawq_bridge.get_map(project_path=project_path, depth=2)
    map_block = rawq_bridge.format_map_block(map_result) if map_result else ""
    if map_block:
        logger.info("rawq.enrich.map_fallback", project=project_name)
        return f"{map_block}\n\n---\n\n{text}"

    logger.info("rawq.enrich.no_results", project=project_name)
    return text


async def handle_code_search(
    backend: TunadishBackend,
    params: dict[str, Any],
    runtime: TransportRuntime,
    transport: TunadishTransport,
) -> None:
    """Handle the code.search RPC."""
    from . import rawq_bridge

    query = params.get("query", "")
    project = params.get("project", "")
    lang = params.get("lang")
    top = params.get("top", 10)

    if not query or not project:
        await transport._send_notification(
            "code.search.result",
            {
                "error": "query and project are required",
            },
        )
        return

    project_path = backend._resolve_project_path(project, runtime)
    if not project_path:
        await transport._send_notification(
            "code.search.result",
            {
                "error": f"Project path not found: {project}",
            },
        )
        return

    result = await rawq_bridge.search(
        query=query,
        project_path=project_path,
        top=top,
        token_budget=8000,
        threshold=0.3,
        lang_filter=lang,
    )

    await transport._send_notification(
        "code.search.result",
        {
            "query": query,
            "project": project,
            "available": rawq_bridge.is_available(),
            "results": result.get("results", []) if result else [],
            "query_ms": result.get("query_ms", 0) if result else 0,
            "total_tokens": result.get("total_tokens", 0) if result else 0,
        },
    )


async def handle_code_map(
    backend: TunadishBackend,
    params: dict[str, Any],
    runtime: TransportRuntime,
    transport: TunadishTransport,
) -> None:
    """Handle the code.map RPC."""
    from . import rawq_bridge

    project = params.get("project", "")
    depth = params.get("depth", 2)
    lang = params.get("lang")

    project_path = backend._resolve_project_path(project, runtime)
    if not project_path:
        await transport._send_notification(
            "code.map.result",
            {
                "error": f"Project path not found: {project}",
            },
        )
        return

    result = await rawq_bridge.get_map(
        project_path=project_path,
        depth=depth,
        lang_filter=lang,
    )

    await transport._send_notification(
        "code.map.result",
        {
            "project": project,
            "available": rawq_bridge.is_available(),
            "map": result if result else {},
        },
    )
