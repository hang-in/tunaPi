"""Slash command handlers for Slack transport."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.chat_command_handlers import (
    handle_branch_command,
    handle_cancel_command,
    handle_context_command,
    handle_help_command,
    handle_memory_command,
    handle_model_command,
    handle_models_command,
    handle_persona_command,
    handle_project_command,
    handle_review_command,
    handle_status_command,
    handle_trigger_command,
)
from ..core.commands import parse_command  # noqa: F401 — re-exported
from ..core.roundtable import handle_rt as _core_handle_rt
from ..logging import get_logger
from .chat_prefs import ChatPrefsStore

if TYPE_CHECKING:
    from ..core.memory_facade import ProjectMemoryFacade

logger = get_logger(__name__)


async def handle_help(
    *,
    runtime: Any,
    send: Any,
) -> None:
    """Show available commands."""
    return await handle_help_command(
        runtime=runtime,
        send=send,
        title="*tunapi commands*",
        subtitle="Use `/command` or `!command` (Slack mobile-friendly).",
        commands_table=[
            "| Command | Description |",
            "|---------|-------------|",
            "| `!help` | Show this help |",
            "| `!new` | Start a new session |",
            "| `!model <engine> [model]` | Switch engine or set model |",
            "| `!models [engine]` | Show available models |",
            "| `!trigger <all|mentions>` | Set trigger mode |",
            "| `!project list|set|info` | Manage project binding |",
            "| `!persona add|list|remove` | Manage personas |",
            "| `!memory [list|add|search|delete]` | Project memory |",
            "| `!branch [create|merge|discard]` | Conversation branches |",
            "| `!review [approve|reject]` | Review requests |",
            "| `!context` | Full project context |",
            '| `!rt "topic"` | Multi-agent roundtable |',
            "| `!file put` | Upload attached files to project |",
            "| `!file get <path>` | Download a file from project |",
            "| `!status` | Show current session info |",
            "| `!cancel` | Cancel running task |",
        ],
        engines_label="*Engines:*",
        projects_label="*Projects:*",
        footer="Prefix a message with `/<engine>` or `/<project>` to target directly.",
    )


async def handle_model(
    args: str,
    *,
    channel_id: str,
    runtime: Any,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
    """Switch engine or set per-engine model."""
    return await handle_model_command(
        args,
        channel_id=channel_id,
        runtime=runtime,
        chat_prefs=chat_prefs,
        send=send,
    )


async def handle_models(
    args: str,
    *,
    channel_id: str,
    runtime: Any,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
    """Show available models per engine."""
    return await handle_models_command(
        args,
        channel_id=channel_id,
        runtime=runtime,
        chat_prefs=chat_prefs,
        send=send,
        title="*Available Models*",
        engine_bold=lambda engine: f"*{engine}*",
    )


async def handle_trigger(
    args: str,
    *,
    channel_id: str,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
    """Set trigger mode for this channel."""
    await handle_trigger_command(
        args,
        channel_id=channel_id,
        chat_prefs=chat_prefs,
        send=send,
        default_mode="mentions",
        usage_command="/trigger",
    )
    logger.info(
        "slack.command.trigger", channel_id=channel_id, mode=args.strip().lower()
    )
    return


async def handle_status(
    *,
    channel_id: str,
    runtime: Any,
    chat_prefs: ChatPrefsStore | None,
    session_engine: str | None,
    has_session: bool,
    send: Any,
) -> None:
    """Show current session info."""
    return await handle_status_command(
        channel_id=channel_id,
        runtime=runtime,
        chat_prefs=chat_prefs,
        has_session=has_session,
        send=send,
        title="*Session status*",
        default_trigger="mentions",
    )


async def handle_project(
    args: str,
    *,
    channel_id: str,
    runtime: Any,
    chat_prefs: ChatPrefsStore | None,
    projects_root: str | None,
    config_path: Path | None = None,
    send: Any,
) -> None:
    """Manage project binding for this channel."""
    return await handle_project_command(
        args,
        channel_id=channel_id,
        runtime=runtime,
        chat_prefs=chat_prefs,
        projects_root=projects_root,
        config_path=config_path,
        send=send,
        title_projects="*Projects*",
        title_channel_project="*Channel project:*",
        title_branch="*Branch:*",
        logger_cb=lambda cid, pkey: logger.info(
            "slack.command.project.set", channel_id=cid, project=pkey
        ),
    )


async def handle_persona(
    args: str,
    *,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
    """Manage persona definitions (global)."""
    return await handle_persona_command(
        args,
        chat_prefs=chat_prefs,
        send=send,
        title_personas="*Personas*",
        fmt_item=lambda name, display: f"- *{name}*: {display}",
        fmt_title=lambda name: f"*{name}*",
        logger_add_cb=lambda name: logger.info("slack.command.persona.add", name=name),
        logger_remove_cb=lambda name: logger.info(
            "slack.command.persona.remove", name=name
        ),
    )


async def handle_rt(
    args: str,
    *,
    runtime: Any,
    send: Any,
    start_roundtable: Any,
    continue_roundtable: Any | None = None,
    close_roundtable: Any | None = None,
    thread_id: str | None = None,
) -> None:
    """Handle ``!rt`` commands — delegates to core implementation."""
    await _core_handle_rt(
        args,
        runtime=runtime,
        send=send,
        start_roundtable=start_roundtable,
        continue_roundtable=continue_roundtable,
        close_roundtable=close_roundtable,
        thread_id=thread_id,
    )


async def handle_memory(
    args: str,
    *,
    project: str | None,
    facade: ProjectMemoryFacade | None,
    current_engine: str | None = None,
    send: Any,
) -> None:
    """Manage project memory entries."""
    return await handle_memory_command(
        args,
        project=project,
        facade=facade,
        current_engine=current_engine,
        send=send,
        title_memory_fmt=lambda proj: f"*Memory — {proj}*",
        title_search_fmt=lambda query: f"*Search results — {query}*",
        fmt_item=lambda e,
        tag_str,
        ts: f"- `{e.id[:16]}` [{e.type}] *{e.title}*{tag_str} ({ts})",
        fmt_search_item=lambda e: f"- `{e.id[:16]}` [{e.type}] *{e.title}*",
    )


async def handle_branch(
    args: str,
    *,
    project: str | None,
    facade: ProjectMemoryFacade | None,
    send: Any,
) -> None:
    """Manage conversation branches."""
    return await handle_branch_command(
        args,
        project=project,
        facade=facade,
        send=send,
        title_active_fmt=lambda proj: f"*Active branches — {proj}*",
        title_branches_fmt=lambda proj: f"*Branches — {proj}*",
        fmt_active_item=lambda b,
        git_tag: f"- `{b.branch_id[:16]}` *{b.label}*{git_tag}",
        fmt_branch_item=lambda b,
        git_tag: f"- `{b.branch_id[:16]}` [{b.status}] *{b.label}*{git_tag}",
        fmt_create_msg=lambda b: f"Branch created: `{b.branch_id[:16]}` *{b.label}*",
        fmt_bold=lambda text: f"*{text}*",
    )


async def handle_review(
    args: str,
    *,
    project: str | None,
    facade: ProjectMemoryFacade | None,
    send: Any,
) -> None:
    """Manage review requests."""
    return await handle_review_command(
        args,
        project=project,
        facade=facade,
        send=send,
        title_pending_fmt=lambda proj: f"*Pending reviews — {proj}*",
        title_reviews_fmt=lambda proj: f"*Reviews — {proj}*",
        fmt_pending_item=lambda r: f"- `{r.review_id[:16]}` artifact `{r.artifact_id[:16]}` v{r.artifact_version} ({r.created_at})",
        fmt_review_item=lambda r: f"- `{r.review_id[:16]}` [{r.status}] artifact `{r.artifact_id[:16]}` v{r.artifact_version}",
    )


async def handle_context(
    *,
    project: str | None,
    facade: ProjectMemoryFacade | None,
    send: Any,
) -> None:
    """Show full project context."""
    return await handle_context_command(
        project=project,
        facade=facade,
        send=send,
    )


async def handle_cancel(
    *,
    channel_id: str,
    running_tasks: dict,
    send: Any,
) -> None:
    """Cancel the running task in this channel."""
    return await handle_cancel_command(
        channel_id=channel_id,
        running_tasks=running_tasks,
        send=send,
    )
