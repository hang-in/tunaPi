"""tunadish command handlers — ! 커맨드 + JSON-RPC 메서드 공용.

Slack/Mattermost handler를 직접 import하지 않고 core 모듈만 사용.
각 handler의 send 콜백은 RenderedMessage를 받아 클라이언트에 전달.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..core.chat_command_handlers import (
    handle_branch_command,
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
from ..engine_models import shorten_model
from ..transport import RenderedMessage
from ..logging import get_logger

if TYPE_CHECKING:
    from ..core.chat_prefs import ChatPrefsStore
    from ..core.memory_facade import ProjectMemoryFacade
    from ..transport_runtime import TransportRuntime
    from ..journal import Journal

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# !help
# ---------------------------------------------------------------------------


async def handle_help(
    *,
    runtime: TransportRuntime,
    send: Any,
) -> None:
    return await handle_help_command(
        runtime=runtime,
        send=send,
        title="**tunapi commands**",
        subtitle=None,
        commands_table=[
            "| Command | Description |",
            "|---------|-------------|",
            "| `!help` | Show this help |",
            "| `!new` | Start a new session |",
            "| `!model <engine> [model]` | Switch engine or set model |",
            "| `!models [engine]` | Show available models |",
            "| `!trigger <all\\|mentions>` | Set trigger mode |",
            "| `!project list\\|set\\|info` | Manage project binding |",
            "| `!persona add\\|list\\|remove` | Manage personas |",
            "| `!memory [list\\|add\\|search\\|delete]` | Project memory |",
            "| `!branch [create\\|merge\\|discard]` | Conversation branches |",
            "| `!review [approve\\|reject]` | Review requests |",
            "| `!context` | Full project context |",
            '| `!rt "topic"` | Multi-agent roundtable |',
            "| `!status` | Show current session info |",
            "| `!cancel` | Cancel running task |",
        ],
        engines_label="**Engines:**",
        projects_label="**Projects:**",
        footer=None,
    )


# ---------------------------------------------------------------------------
# !model / !models
# ---------------------------------------------------------------------------


async def handle_model(
    args: str,
    *,
    channel_id: str,
    runtime: TransportRuntime,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
    return await handle_model_command(
        args,
        channel_id=channel_id,
        runtime=runtime,
        chat_prefs=chat_prefs,
        send=send,
        describe_model=shorten_model,
        include_models_hint=False,
    )


async def handle_models(
    args: str,
    *,
    channel_id: str,
    runtime: TransportRuntime,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
    return await handle_models_command(
        args,
        channel_id=channel_id,
        runtime=runtime,
        chat_prefs=chat_prefs,
        send=send,
        title="**Available Models**",
        engine_bold=lambda engine: f"**{engine}**",
    )


async def handle_trigger(
    args: str,
    *,
    channel_id: str,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
    return await handle_trigger_command(
        args,
        channel_id=channel_id,
        chat_prefs=chat_prefs,
        send=send,
        default_mode="mentions",
        usage_command="!trigger",
    )
    mode = args.strip().lower()

    if mode not in ("all", "mentions"):
        current = "mentions"
        if chat_prefs:
            current = await chat_prefs.get_trigger_mode(channel_id) or "mentions"
        await send(
            RenderedMessage(
                text=f"Current trigger mode: `{current}`\n\nUsage: `!trigger all` or `!trigger mentions`"
            )
        )
        return


async def handle_status(
    *,
    channel_id: str,
    runtime: TransportRuntime,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
    return await handle_status_command(
        channel_id=channel_id,
        runtime=runtime,
        chat_prefs=chat_prefs,
        has_session=None,
        send=send,
        title="**Session status**",
        default_trigger="mentions",
    )


async def handle_project(
    args: str,
    *,
    channel_id: str,
    runtime: TransportRuntime,
    chat_prefs: ChatPrefsStore | None,
    context_store: Any | None = None,
    projects_root: str | None,
    config_path: Path | None = None,
    send: Any,
) -> None:
    # context_store가 존재하면 chat_prefs 대신 context_store를 사용
    prefs = context_store if context_store is not None else chat_prefs
    return await handle_project_command(
        args,
        channel_id=channel_id,
        runtime=runtime,
        chat_prefs=prefs,
        projects_root=projects_root,
        config_path=config_path,
        send=send,
        title_projects="**Projects**",
        title_channel_project="**Channel project:**",
        title_branch="**Branch:**",
        title_no_bound="No project bound.",
        success_msg_fmt=lambda pkey: f"Project set to `{pkey}`.",
        set_context_filter=lambda cid: cid != "__rpc__",
    )


# ---------------------------------------------------------------------------
# !persona
# ---------------------------------------------------------------------------


async def handle_persona(
    args: str,
    *,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
    return await handle_persona_command(
        args,
        chat_prefs=chat_prefs,
        send=send,
        title_personas="**Personas**",
        fmt_item=lambda name, display: f"- **{name}**: {display}",
        fmt_title=lambda name: f"**{name}**",
    )


# ---------------------------------------------------------------------------
# !memory
# ---------------------------------------------------------------------------


async def handle_memory(
    args: str,
    *,
    project: str | None,
    facade: ProjectMemoryFacade | None,
    current_engine: str | None = None,
    send: Any,
) -> None:
    return await handle_memory_command(
        args,
        project=project,
        facade=facade,
        current_engine=current_engine,
        send=send,
        title_memory_fmt=lambda proj: f"**Memory — {proj}**",
        title_search_fmt=lambda query: f"**Search results — {query}**",
        fmt_item=lambda e,
        tag_str,
        ts: f"- `{e.id[:16]}` [{e.type}] **{e.title}**{tag_str} ({ts})",
        fmt_search_item=lambda e: f"- `{e.id[:16]}` [{e.type}] **{e.title}**",
    )


# ---------------------------------------------------------------------------
# !branch
# ---------------------------------------------------------------------------


async def handle_branch(
    args: str,
    *,
    project: str | None,
    facade: ProjectMemoryFacade | None,
    send: Any,
) -> None:
    return await handle_branch_command(
        args,
        project=project,
        facade=facade,
        send=send,
        title_active_fmt=lambda proj: f"**Active branches — {proj}**",
        title_branches_fmt=lambda proj: f"**Branches — {proj}**",
        fmt_active_item=lambda b,
        git_tag: f"- `{b.branch_id[:16]}` **{b.label}**{git_tag}",
        fmt_branch_item=lambda b,
        git_tag: f"- `{b.branch_id[:16]}` [{b.status}] **{b.label}**{git_tag}",
        fmt_create_msg=lambda b: f"Branch created: `{b.branch_id[:16]}` **{b.label}**",
        fmt_bold=lambda text: f"**{text}**",
    )


# ---------------------------------------------------------------------------
# !review
# ---------------------------------------------------------------------------


async def handle_review(
    args: str,
    *,
    project: str | None,
    facade: ProjectMemoryFacade | None,
    send: Any,
) -> None:
    return await handle_review_command(
        args,
        project=project,
        facade=facade,
        send=send,
        title_pending_fmt=lambda proj: f"**Pending reviews — {proj}**",
        title_reviews_fmt=lambda proj: f"**Reviews — {proj}**",
        fmt_pending_item=lambda r: f"- `{r.review_id[:16]}` artifact `{r.artifact_id[:16]}` v{r.artifact_version}",
        fmt_review_item=lambda r: f"- `{r.review_id[:16]}` [{r.status}] artifact `{r.artifact_id[:16]}` v{r.artifact_version}",
    )


# ---------------------------------------------------------------------------
# !context
# ---------------------------------------------------------------------------


async def handle_context(
    *,
    project: str | None,
    facade: ProjectMemoryFacade | None,
    send: Any,
) -> None:
    return await handle_context_command(
        project=project,
        facade=facade,
        send=send,
    )


# ---------------------------------------------------------------------------
# !rt (roundtable) — placeholder, full implementation is a separate sprint
# ---------------------------------------------------------------------------


async def handle_rt(
    args: str,
    *,
    runtime: TransportRuntime,
    send: Any,
) -> None:
    rt_config = runtime.roundtable
    rt_engines = list(rt_config.engines) or list(runtime.available_engine_ids())
    engines_display = ", ".join(f"`{e}`" for e in rt_engines)

    await send(
        RenderedMessage(
            text=(
                "**Roundtable** — 멀티에이전트 토론\n\n"
                "tunaDish RT 모드는 별도 스프린트에서 구현 예정입니다.\n"
                "현재 사용 가능한 엔진: " + engines_display + "\n\n"
                "Usage:\n"
                '- `!rt "topic"` — 새 라운드테이블\n'
                '- `!rt follow [engines] "question"` — 후속 질문\n'
                "- `!rt close` — 라운드테이블 종료"
            )
        )
    )


# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------


async def dispatch_command(
    cmd: str,
    args: str,
    *,
    channel_id: str,
    runtime: TransportRuntime,
    chat_prefs: ChatPrefsStore | None,
    facade: ProjectMemoryFacade | None,
    journal: Journal | None,
    context_store: Any | None = None,
    conv_sessions: Any | None = None,
    running_tasks: dict,
    projects_root: str | None = None,
    config_path: Path | None = None,
    send: Any,
) -> bool:
    """Dispatch a parsed ! command. Returns True if handled, False if unknown."""

    async def _get_project() -> str | None:
        # context_store가 source of truth (conv별 project binding)
        if context_store:
            ctx = await context_store.get_context(channel_id)
            if ctx and ctx.project:
                return ctx.project
        return None

    async def _get_engine() -> str | None:
        if chat_prefs:
            return await chat_prefs.get_default_engine(channel_id)
        return None

    match cmd:
        case "new":
            if journal:
                await journal.mark_reset(channel_id)
            if conv_sessions:
                await conv_sessions.clear(channel_id)
            await send(RenderedMessage(text="새 대화를 시작합니다."))
        case "help":
            await handle_help(runtime=runtime, send=send)
        case "model":
            await handle_model(
                args,
                channel_id=channel_id,
                runtime=runtime,
                chat_prefs=chat_prefs,
                send=send,
            )
        case "models":
            await handle_models(
                args,
                channel_id=channel_id,
                runtime=runtime,
                chat_prefs=chat_prefs,
                send=send,
            )
        case "trigger":
            await handle_trigger(
                args,
                channel_id=channel_id,
                chat_prefs=chat_prefs,
                send=send,
            )
        case "project":
            await handle_project(
                args,
                channel_id=channel_id,
                runtime=runtime,
                chat_prefs=chat_prefs,
                context_store=context_store,
                projects_root=projects_root,
                config_path=config_path,
                send=send,
            )
        case "persona":
            await handle_persona(args, chat_prefs=chat_prefs, send=send)
        case "memory":
            project = await _get_project()
            engine = await _get_engine()
            await handle_memory(
                args,
                project=project,
                facade=facade,
                current_engine=engine or runtime.default_engine,
                send=send,
            )
        case "branch":
            project = await _get_project()
            await handle_branch(args, project=project, facade=facade, send=send)
        case "review":
            project = await _get_project()
            await handle_review(args, project=project, facade=facade, send=send)
        case "context":
            project = await _get_project()
            await handle_context(project=project, facade=facade, send=send)
        case "rt":
            await handle_rt(args, runtime=runtime, send=send)
        case "status":
            await handle_status(
                channel_id=channel_id,
                runtime=runtime,
                chat_prefs=chat_prefs,
                send=send,
            )
        case "cancel":
            cancelled = False
            for ref, task in list(running_tasks.items()):
                if str(ref.channel_id) == channel_id:
                    task.cancel_requested.set()
                    cancelled = True
                    break
            if cancelled:
                await send(RenderedMessage(text="Task cancelled."))
            else:
                await send(RenderedMessage(text="No running task to cancel."))
        case _:
            return False

    return True
