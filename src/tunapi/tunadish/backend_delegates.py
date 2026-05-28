from __future__ import annotations

from typing import Any


async def _handle_project_context(
    self: Any, params: dict[str, Any], runtime: Any, transport: Any
):
    from . import context_handlers

    await context_handlers.handle_project_context(self, params, runtime, transport)


async def _handle_branch_list_json(
    self: Any, params: dict[str, Any], runtime: Any, transport: Any
):
    from . import context_handlers

    await context_handlers.handle_branch_list_json(self, params, runtime, transport)


async def _handle_memory_list_json(self: Any, params: dict[str, Any], transport: Any):
    from . import context_handlers

    await context_handlers.handle_memory_list_json(self, params, transport)


async def _handle_review_list_json(self: Any, params: dict[str, Any], transport: Any):
    from . import context_handlers

    await context_handlers.handle_review_list_json(self, params, transport)


async def _handle_branch_create(self: Any, params: dict[str, Any], transport: Any):
    from . import branch_handlers

    await branch_handlers.handle_branch_create(self, params, transport)


async def _handle_branch_switch(self: Any, params: dict[str, Any], transport: Any):
    from . import branch_handlers

    await branch_handlers.handle_branch_switch(self, params, transport)


async def _handle_branch_adopt(self: Any, params: dict[str, Any], transport: Any):
    from . import branch_handlers

    await branch_handlers.handle_branch_adopt(self, params, transport)


async def _build_adopt_summary(self: Any, branch: Any, conv_id: str) -> str:
    from . import branch_handlers

    return await branch_handlers.build_adopt_summary(self, branch, conv_id)


async def _build_branch_context(
    self: Any, conv_id: str, checkpoint_id: str | None
) -> str:
    from . import branch_handlers

    return await branch_handlers.build_branch_context(self, conv_id, checkpoint_id)


async def _handle_branch_archive(self: Any, params: dict[str, Any], transport: Any):
    from . import branch_handlers

    await branch_handlers.handle_branch_archive(self, params, transport)


async def _handle_branch_delete(self: Any, params: dict[str, Any], transport: Any):
    from . import branch_handlers

    await branch_handlers.handle_branch_delete(self, params, transport)


async def _handle_message_retry(
    self: Any,
    params: dict[str, Any],
    runtime: Any,
    transport: Any,
    ws_tg: Any,
):
    from . import message_handlers

    await message_handlers.handle_message_retry(self, params, runtime, transport, ws_tg)


async def _handle_message_save(self: Any, params: dict[str, Any], transport: Any):
    from . import message_handlers

    await message_handlers.handle_message_save(self, params, transport)


async def _handle_message_delete(self: Any, params: dict[str, Any], transport: Any):
    from . import message_handlers

    await message_handlers.handle_message_delete(self, params, transport)


async def _handle_message_adopt(self: Any, params: dict[str, Any], transport: Any):
    from . import message_handlers

    await message_handlers.handle_message_adopt(self, params, transport)


async def _rawq_startup_check(self: Any):
    from . import rawq_handlers

    await rawq_handlers.rawq_startup_check(self)


def _resolve_project_path(self: Any, project_name: str, runtime: Any):
    from pathlib import Path

    projects_map = getattr(getattr(runtime, "_projects", None), "projects", {})
    pc = projects_map.get(project_name.lower())
    if pc and getattr(pc, "path", None) and Path(pc.path).exists():
        return Path(pc.path)

    projects_root = self._get_projects_root()
    if projects_root:
        candidate = Path(projects_root).expanduser() / project_name
        if candidate.exists():
            return candidate

    return None


async def _rawq_ensure_index(
    self: Any, project_name: str, runtime: Any, transport: Any
):
    from . import rawq_handlers

    await rawq_handlers.rawq_ensure_index(self, project_name, runtime, transport)


async def _rawq_enrich_message(self: Any, text: str, project_name: str, runtime: Any):
    from . import rawq_handlers

    return await rawq_handlers.rawq_enrich_message(self, text, project_name, runtime)


async def _handle_code_search(
    self: Any, params: dict[str, Any], runtime: Any, transport: Any
):
    from . import rawq_handlers

    await rawq_handlers.handle_code_search(self, params, runtime, transport)


async def _handle_code_map(
    self: Any, params: dict[str, Any], runtime: Any, transport: Any
):
    from . import rawq_handlers

    await rawq_handlers.handle_code_map(self, params, runtime, transport)


async def _handle_discussion_save(self: Any, params: dict[str, Any], transport: Any):
    from . import message_handlers

    await message_handlers.handle_discussion_save(self, params, transport)


async def _handle_discussion_link_branch(
    self: Any, params: dict[str, Any], transport: Any
):
    from . import message_handlers

    await message_handlers.handle_discussion_link_branch(self, params, transport)


async def _handle_synthesis_create(self: Any, params: dict[str, Any], transport: Any):
    from . import message_handlers

    await message_handlers.handle_synthesis_create(self, params, transport)


async def _handle_review_request(self: Any, params: dict[str, Any], transport: Any):
    from . import message_handlers

    await message_handlers.handle_review_request(self, params, transport)


async def _handle_handoff_create(
    self: Any, params: dict[str, Any], runtime: Any, transport: Any
):
    from . import message_handlers

    await message_handlers.handle_handoff_create(self, params, runtime, transport)


async def _handle_handoff_parse(self: Any, params: dict[str, Any], transport: Any):
    from . import message_handlers

    await message_handlers.handle_handoff_parse(self, params, transport)


async def _handle_engine_list(self: Any, runtime: Any, transport: Any):
    from . import message_handlers

    await message_handlers.handle_engine_list(self, runtime, transport)


DELEGATE_METHODS = (
    "_handle_project_context",
    "_handle_branch_list_json",
    "_handle_memory_list_json",
    "_handle_review_list_json",
    "_handle_branch_create",
    "_handle_branch_switch",
    "_handle_branch_adopt",
    "_build_adopt_summary",
    "_build_branch_context",
    "_handle_branch_archive",
    "_handle_branch_delete",
    "_handle_message_retry",
    "_handle_message_save",
    "_handle_message_delete",
    "_handle_message_adopt",
    "_rawq_startup_check",
    "_resolve_project_path",
    "_rawq_ensure_index",
    "_rawq_enrich_message",
    "_handle_code_search",
    "_handle_code_map",
    "_handle_discussion_save",
    "_handle_discussion_link_branch",
    "_handle_synthesis_create",
    "_handle_review_request",
    "_handle_handoff_create",
    "_handle_handoff_parse",
    "_handle_engine_list",
)
