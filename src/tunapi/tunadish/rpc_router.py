"""JSON-RPC method routing tables for the tunadish backend.

Replaces the former ~40-branch ``if/elif`` chain in ``backend._ws_handler``
with declarative tables. Three method classes:

- **special** — unique control flow (ping/chat.send/run.cancel/project.set/
  model.set/roundtable.start); handled explicitly in ``backend._dispatch_rpc``.
- **command** (`COMMAND_ROUTES`) — thin wrappers over the shared ``!``-command
  system: ``method -> (cmd, args_string)`` built from ``params``.
- **direct** (`DIRECT_ROUTES`) — call a backend/handler coroutine. Each value
  takes a single ``RpcCall`` and forwards only the args its target needs.

``NO_AUTO_RPC_ID`` lists methods whose first outgoing notification must NOT be
auto-converted into the JSON-RPC response (streaming / fire-and-forget).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import context_handlers

# Methods that manage their own response (or none) — the receive loop must not
# arm ``transport.set_rpc_id`` for these, or their first streamed message.new
# would be swallowed into the JSON-RPC response.
NO_AUTO_RPC_ID = frozenset({"ping", "chat.send", "run.cancel", "roundtable.start"})


@dataclass(frozen=True, slots=True)
class RpcCall:
    """Everything a direct route handler may need for one request."""

    backend: Any
    params: dict[str, Any]
    runtime: Any
    transport: Any
    ws_tg: Any


# method -> (cmd, args_string) over backend._dispatch_rpc_command
COMMAND_ROUTES: dict[str, Callable[[dict[str, Any]], tuple[str, str]]] = {
    "help": lambda p: ("help", ""),
    "model.list": lambda p: ("models", p.get("engine", "")),
    "trigger.set": lambda p: ("trigger", p.get("mode", "")),
    "project.info": lambda p: ("project", "info"),
    "persona.set": lambda p: ("persona", p.get("args", "")),
    "persona.list": lambda p: ("persona", "list"),
    "memory.list": lambda p: ("memory", f"list {p.get('type', '')}".strip()),
    "memory.add": lambda p: (
        "memory",
        f"add {p.get('type', '')} {p.get('title', '')} {p.get('content', '')}",
    ),
    "memory.search": lambda p: ("memory", f"search {p.get('query', '')}"),
    "memory.delete": lambda p: ("memory", f"delete {p.get('id', '')}"),
    "branch.list": lambda p: ("branch", f"list {p.get('status', '')}".strip()),
    "branch.merge": lambda p: ("branch", f"merge {p.get('id', '')}"),
    "branch.discard": lambda p: ("branch", f"discard {p.get('id', '')}"),
    "review.list": lambda p: ("review", f"list {p.get('status', '')}".strip()),
    "review.approve": lambda p: (
        "review",
        f"approve {p.get('id', '')} {p.get('comment', '')}".strip(),
    ),
    "review.reject": lambda p: (
        "review",
        f"reject {p.get('id', '')} {p.get('comment', '')}".strip(),
    ),
    "context.get": lambda p: ("context", ""),
    "session.new": lambda p: ("new", ""),
    "status": lambda p: ("status", ""),
}


DirectRoute = Callable[[RpcCall], Awaitable[None]]

# method -> direct handler. Bound ``backend._handle_*`` methods are kept (each is
# covered by unit tests); context_handlers without a bound wrapper are called
# directly here.
DIRECT_ROUTES: dict[str, DirectRoute] = {
    # context / conversation
    "project.list": lambda c: context_handlers.handle_project_list(
        c.backend, c.params, c.runtime, c.transport
    ),
    "conversation.create": lambda c: context_handlers.handle_conversation_create(
        c.backend, c.params, c.transport
    ),
    "conversation.delete": lambda c: context_handlers.handle_conversation_delete(
        c.backend, c.params, c.transport
    ),
    "conversation.list": lambda c: context_handlers.handle_conversation_list(
        c.backend, c.params, c.runtime, c.transport
    ),
    "conversation.history": lambda c: context_handlers.handle_conversation_history(
        c.backend, c.params, c.transport
    ),
    # structured JSON (context panel)
    "project.context": lambda c: c.backend._handle_project_context(
        c.params, c.runtime, c.transport
    ),
    "branch.list.json": lambda c: c.backend._handle_branch_list_json(
        c.params, c.runtime, c.transport
    ),
    "memory.list.json": lambda c: c.backend._handle_memory_list_json(
        c.params, c.transport
    ),
    "review.list.json": lambda c: c.backend._handle_review_list_json(
        c.params, c.transport
    ),
    # rawq code search/map
    "code.search": lambda c: c.backend._handle_code_search(
        c.params, c.runtime, c.transport
    ),
    "code.map": lambda c: c.backend._handle_code_map(c.params, c.runtime, c.transport),
    # branch actions
    "branch.create": lambda c: c.backend._handle_branch_create(c.params, c.transport),
    "branch.switch": lambda c: c.backend._handle_branch_switch(c.params, c.transport),
    "branch.adopt": lambda c: c.backend._handle_branch_adopt(c.params, c.transport),
    "branch.archive": lambda c: c.backend._handle_branch_archive(c.params, c.transport),
    "branch.delete": lambda c: c.backend._handle_branch_delete(c.params, c.transport),
    # message actions
    "message.retry": lambda c: c.backend._handle_message_retry(
        c.params, c.runtime, c.transport, c.ws_tg
    ),
    "message.save": lambda c: c.backend._handle_message_save(c.params, c.transport),
    "message.delete": lambda c: c.backend._handle_message_delete(c.params, c.transport),
    "message.adopt": lambda c: c.backend._handle_message_adopt(c.params, c.transport),
    # phase 4: write API + handoff
    "discussion.save_roundtable": lambda c: c.backend._handle_discussion_save(
        c.params, c.transport
    ),
    "discussion.link_branch": lambda c: c.backend._handle_discussion_link_branch(
        c.params, c.transport
    ),
    "synthesis.create_from_discussion": lambda c: c.backend._handle_synthesis_create(
        c.params, c.transport
    ),
    "review.request": lambda c: c.backend._handle_review_request(c.params, c.transport),
    "handoff.create": lambda c: c.backend._handle_handoff_create(
        c.params, c.runtime, c.transport
    ),
    "handoff.parse": lambda c: c.backend._handle_handoff_parse(c.params, c.transport),
    "engine.list": lambda c: c.backend._handle_engine_list(c.runtime, c.transport),
}
