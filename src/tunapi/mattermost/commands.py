"""Slash command handlers for Mattermost transport.

Mattermost's native slash commands require external integration URLs.
Instead, we detect `/command` prefixes in regular messages and handle
them before passing to the engine dispatcher.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import HOME_CONFIG_PATH, ConfigError, read_config, write_config
from ..context import RunContext
from ..core.chat_command_handlers import (
    handle_cancel_command,
    handle_model_command,
    handle_models_command,
    handle_status_command,
    handle_trigger_command,
)
from ..core.commands import parse_command
from ..logging import get_logger
from ..transport import RenderedMessage
from .chat_prefs import ChatPrefsStore

if TYPE_CHECKING:
    from ..core.memory_facade import ProjectMemoryFacade

logger = get_logger(__name__)

# Keep backward-compatible alias
parse_slash_command = parse_command


async def handle_help(
    *,
    runtime: Any,
    send: Any,
) -> None:
    """Show available commands."""
    engines = list(runtime.available_engine_ids())
    projects = sorted(set(runtime.project_aliases()), key=str.lower)

    lines = [
        "**tunapi commands**",
        "",
        "Use `/command` or `!command` (mobile-friendly).",
        "",
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
        '| `!rt "주제"` | Multi-agent roundtable |',
        "| `!file put` | Upload attached files to project |",
        "| `!file get <path>` | Download a file from project |",
        "| `!status` | Show current session info |",
        "| `!cancel` | Cancel running task |",
        "",
        f"**Engines:** {', '.join(f'`{e}`' for e in engines) or 'none'}",
        "",
        f"**Projects:** {', '.join(f'`{p}`' for p in projects) or 'none'}",
        "",
        "Prefix a message with `/<engine>` or `/<project>` to target directly.",
    ]
    await send(RenderedMessage(text="\n".join(lines)))


async def handle_model(
    args: str,
    *,
    channel_id: str,
    runtime: Any,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
    """Switch engine or set per-engine model.

    - ``!model`` — show current engine
    - ``!model <engine>`` — switch default engine (or show model if already current)
    - ``!model <engine> <model>`` — set model for engine
    - ``!model <engine> clear`` — clear model override
    """
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
    """Show available models per engine.

    - ``!models`` — all engines
    - ``!models <engine>`` — specific engine
    """
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
    """Set trigger mode for this channel."""
    await handle_trigger_command(
        args,
        channel_id=channel_id,
        chat_prefs=chat_prefs,
        send=send,
        default_mode="all",
        usage_command="/trigger",
    )
    logger.info("command.trigger", channel_id=channel_id, mode=args.strip().lower())
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
        title="**Session status**",
        default_trigger="all",
    )


def _register_project_in_config(
    name: str,
    path: Path,
    channel_id: str,
    *,
    runtime: Any,
    config_path: Path | None,
) -> None:
    """Discovered 프로젝트를 toml에 등록하고 런타임 in-memory 상태도 업데이트."""
    cfg_path = config_path or HOME_CONFIG_PATH
    try:
        config = read_config(cfg_path)
        projects = config.setdefault("projects", {})
        if name not in projects:
            projects[name] = {"path": str(path.resolve())}
            write_config(config, cfg_path)
    except ConfigError:
        pass
    runtime._projects.register_discovered(name, path.resolve(), channel_id)


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
    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""
    subargs = parts[1].strip() if len(parts) > 1 else ""

    if subcmd == "list":
        # List configured projects + discovered from projects_root
        configured = sorted(set(runtime.project_aliases()), key=str.lower)
        discovered: list[str] = []
        if projects_root:
            root = Path(projects_root).expanduser()
            if root.is_dir():
                discovered = sorted(
                    d.name
                    for d in root.iterdir()
                    if d.is_dir()
                    and (d / ".git").exists()
                    and d.name.lower() not in {c.lower() for c in configured}
                )

        lines = ["**Projects**", ""]
        if configured:
            lines.append("Configured: " + ", ".join(f"`{p}`" for p in configured))
        if discovered:
            lines.append("Discovered: " + ", ".join(f"`{p}`" for p in discovered))
        if not configured and not discovered:
            lines.append("No projects found.")
        lines.extend(["", "Usage: `!project set <name>`"])
        await send(RenderedMessage(text="\n".join(lines)))
        return

    if subcmd == "set":
        if not subargs:
            await send(RenderedMessage(text="Usage: `!project set <name>`"))
            return

        name = subargs.lower()
        # Check configured projects first
        project_key = runtime.normalize_project_key(name)
        discovered_path: Path | None = None

        # Check discovered projects in projects_root (case-insensitive)
        if project_key is None and projects_root:
            root = Path(projects_root).expanduser()
            if root.is_dir():
                for candidate in root.iterdir():
                    if (
                        candidate.is_dir()
                        and candidate.name.lower() == name
                        and (candidate / ".git").exists()
                    ):
                        project_key = name
                        discovered_path = candidate
                        break

        if project_key is None:
            await send(
                RenderedMessage(
                    text=f"Unknown project `{name}`. Use `!project list` to see available projects."
                )
            )
            return

        # Discovered 프로젝트는 toml에 자동 등록
        if discovered_path is not None:
            _register_project_in_config(
                name,
                discovered_path,
                channel_id,
                runtime=runtime,
                config_path=config_path,
            )

        if chat_prefs:
            await chat_prefs.set_context(channel_id, RunContext(project=project_key))
        await send(
            RenderedMessage(text=f"Project set to `{project_key}` for this channel.")
        )
        logger.info("command.project.set", channel_id=channel_id, project=project_key)
        return

    if subcmd == "info":
        ctx = None
        if chat_prefs:
            ctx = await chat_prefs.get_context(channel_id)

        if ctx and ctx.project:
            lines = [
                f"**Channel project:** `{ctx.project}`",
            ]
            if ctx.branch:
                lines.append(f"**Branch:** `{ctx.branch}`")
        else:
            lines = [
                "No project bound to this channel.",
                "",
                "Usage: `!project set <name>`",
            ]
        await send(RenderedMessage(text="\n".join(lines)))
        return

    # Default: show usage
    await send(
        RenderedMessage(
            text="Usage: `!project list` | `!project set <name>` | `!project info`"
        )
    )


async def handle_persona(
    args: str,
    *,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
    """Manage persona definitions (global)."""
    if not chat_prefs:
        await send(RenderedMessage(text="Persona storage unavailable."))
        return

    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""
    subargs = parts[1].strip() if len(parts) > 1 else ""

    if subcmd == "add":
        # !persona add <name> "prompt text"  or  !persona add <name> prompt text
        add_parts = subargs.split(None, 1)
        if len(add_parts) < 2:
            await send(RenderedMessage(text='Usage: `!persona add <name> "<prompt>"`'))
            return
        name = add_parts[0].lower()
        prompt = add_parts[1].strip().strip('"').strip("'")
        if not prompt:
            await send(RenderedMessage(text='Usage: `!persona add <name> "<prompt>"`'))
            return
        await chat_prefs.add_persona(name, prompt)
        await send(RenderedMessage(text=f"Persona `{name}` added."))
        logger.info("command.persona.add", name=name)
        return

    if subcmd == "list":
        personas = await chat_prefs.list_personas()
        if not personas:
            await send(
                RenderedMessage(
                    text='No personas defined. Use `!persona add <name> "<prompt>"`'
                )
            )
            return
        lines = ["**Personas**", ""]
        for name, p in sorted(personas.items()):
            # Truncate long prompts for display
            display = p.prompt if len(p.prompt) <= 80 else p.prompt[:77] + "..."
            lines.append(f"- **{name}**: {display}")
        await send(RenderedMessage(text="\n".join(lines)))
        return

    if subcmd == "remove":
        name = subargs.strip().lower()
        if not name:
            await send(RenderedMessage(text="Usage: `!persona remove <name>`"))
            return
        removed = await chat_prefs.remove_persona(name)
        if removed:
            await send(RenderedMessage(text=f"Persona `{name}` removed."))
            logger.info("command.persona.remove", name=name)
        else:
            await send(RenderedMessage(text=f"Persona `{name}` not found."))
        return

    if subcmd == "show":
        name = subargs.strip().lower()
        if not name:
            await send(RenderedMessage(text="Usage: `!persona show <name>`"))
            return
        persona = await chat_prefs.get_persona(name)
        if persona:
            await send(RenderedMessage(text=f"**{persona.name}**\n\n{persona.prompt}"))
        else:
            await send(RenderedMessage(text=f"Persona `{name}` not found."))
        return

    # Default: show usage
    await send(
        RenderedMessage(
            text='Usage: `!persona add <name> "<prompt>"` | `!persona list` | `!persona show <name>` | `!persona remove <name>`'
        )
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
    """Handle ``!rt`` commands.

    - ``!rt "topic" [--rounds N]`` — start a new roundtable
    - ``!rt follow [engines] "topic"`` — follow-up in completed roundtable thread
    - ``!rt close`` — close the current roundtable thread
    """
    from .roundtable import parse_followup_args, parse_rt_args

    rt_config = runtime.roundtable
    rt_engines = list(rt_config.engines) or list(runtime.available_engine_ids())

    if not rt_engines:
        await send(RenderedMessage(text="⚠️ No engines available for roundtable."))
        return

    stripped = args.strip()

    # Check for "close" subcommand
    if stripped.lower().startswith("close"):
        if not close_roundtable:
            await send(
                RenderedMessage(
                    text="⚠️ `!rt close`는 라운드테이블 스레드에서만 사용할 수 있습니다."
                )
            )
            return
        await close_roundtable()
        return

    # Check for "follow" subcommand
    if stripped.lower().startswith("follow"):
        follow_args = stripped[len("follow") :].strip()
        if not continue_roundtable:
            await send(
                RenderedMessage(
                    text="⚠️ `!rt follow`는 완료된 라운드테이블 스레드에서만 사용할 수 있습니다."
                )
            )
            return

        topic, engines_filter, error = parse_followup_args(follow_args, rt_engines)
        if error:
            await send(RenderedMessage(text=f"⚠️ {error}"))
            return
        if not topic:
            engines_display = ", ".join(f"`{e}`" for e in rt_engines)
            await send(
                RenderedMessage(
                    text=(
                        "**Roundtable Follow-up** — 완료된 토론에 후속 질문\n\n"
                        "Usage:\n"
                        '- `!rt follow "질문"` — 전체 에이전트\n'
                        '- `!rt follow claude "질문"` — 특정 에이전트\n'
                        '- `!rt follow gemini,claude "질문"` — 복수 지정\n\n'
                        f"Engines: {engines_display}"
                    )
                )
            )
            return

        await continue_roundtable(topic, engines_filter)
        return

    # Default: start a new roundtable
    topic, rounds, error = parse_rt_args(args, rt_config)

    if error:
        await send(RenderedMessage(text=f"⚠️ {error}"))
        return
    if not topic:
        engines_display = ", ".join(f"`{e}`" for e in rt_engines)
        await send(
            RenderedMessage(
                text=(
                    "**Roundtable** — 여러 에이전트의 의견을 순차 수집\n\n"
                    "Usage:\n"
                    '- `!rt "주제"` — 새 라운드테이블\n'
                    '- `!rt "주제" --rounds 2` — 다중 라운드\n'
                    '- `!rt follow [에이전트] "질문"` — 후속 토론\n'
                    "- `!rt close` — 라운드테이블 종료\n\n"
                    f"Engines: {engines_display}\n"
                    f"Default rounds: {rt_config.rounds} (max {rt_config.max_rounds})"
                )
            )
        )
        return

    await start_roundtable(topic, rounds, rt_engines)


async def handle_memory(
    args: str,
    *,
    project: str | None,
    facade: ProjectMemoryFacade | None,
    current_engine: str | None = None,
    send: Any,
) -> None:
    """Manage project memory entries."""
    if not project:
        await send(
            RenderedMessage(text="프로젝트를 먼저 설정하세요. `!project set <name>`")
        )
        return
    if not facade:
        await send(RenderedMessage(text="Memory storage unavailable."))
        return

    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""
    subargs = parts[1].strip() if len(parts) > 1 else ""

    if not subcmd:
        summary = await facade.memory.get_context_summary(project, max_per_type=5)
        if not summary:
            await send(
                RenderedMessage(
                    text=f"프로젝트 `{project}`에 저장된 메모리가 없습니다."
                )
            )
        else:
            await send(RenderedMessage(text=summary))
        return

    if subcmd == "list":
        entry_type = subargs.lower() if subargs else None
        valid_types = ("decision", "review", "idea", "context")
        if entry_type and entry_type not in valid_types:
            await send(
                RenderedMessage(
                    text=f"Unknown type `{entry_type}`. Available: {', '.join(f'`{t}`' for t in valid_types)}"
                )
            )
            return
        entries = await facade.memory.list_entries(project, type=entry_type, limit=20)
        if not entries:
            label = f" ({entry_type})" if entry_type else ""
            await send(RenderedMessage(text=f"No entries{label} in `{project}`."))
            return
        lines = [f"**Memory — {project}**", ""]
        for e in entries:
            ts = time.strftime("%m/%d", time.localtime(e.timestamp))
            tag_str = f" [{', '.join(e.tags)}]" if e.tags else ""
            lines.append(f"- `{e.id[:16]}` [{e.type}] **{e.title}**{tag_str} ({ts})")
        await send(RenderedMessage(text="\n".join(lines)))
        return

    if subcmd == "add":
        add_parts = subargs.split(None, 2)
        if len(add_parts) < 3:
            await send(
                RenderedMessage(
                    text="Usage: `!memory add <type> <title> <content>`\nTypes: `decision`, `review`, `idea`, `context`"
                )
            )
            return
        entry_type_raw, title, content = add_parts
        entry_type_raw = entry_type_raw.lower()
        valid_types = ("decision", "review", "idea", "context")
        if entry_type_raw not in valid_types:
            await send(
                RenderedMessage(
                    text=f"Unknown type `{entry_type_raw}`. Available: {', '.join(f'`{t}`' for t in valid_types)}"
                )
            )
            return
        source = current_engine or "user"
        entry = await facade.memory.add_entry(
            project,
            type=entry_type_raw,  # type: ignore[arg-type]
            title=title,
            content=content,
            source=source,
        )
        await send(
            RenderedMessage(
                text=f"Entry added: `{entry.id[:16]}` [{entry.type}] **{entry.title}** (source: {source})"
            )
        )
        return

    if subcmd == "search":
        if not subargs:
            await send(RenderedMessage(text="Usage: `!memory search <query>`"))
            return
        results = await facade.memory.search(project, subargs)
        if not results:
            await send(RenderedMessage(text=f"No results for `{subargs}`."))
            return
        lines = [f"**Search results — {subargs}**", ""]
        for e in results[:10]:
            lines.append(f"- `{e.id[:16]}` [{e.type}] **{e.title}**")
        await send(RenderedMessage(text="\n".join(lines)))
        return

    if subcmd == "delete":
        if not subargs:
            await send(RenderedMessage(text="Usage: `!memory delete <id>`"))
            return
        entry_id, err = await _resolve_id(
            subargs,
            fetch_all=lambda: facade.memory.list_entries(project, limit=200),
            get_id=lambda e: e.id,
            get_label=lambda e: e.title,
        )
        if err:
            await send(RenderedMessage(text=err))
            return
        assert entry_id is not None
        deleted = await facade.memory.delete_entry(project, entry_id)
        if deleted:
            await send(RenderedMessage(text=f"Entry `{entry_id[:16]}` deleted."))
        else:
            await send(RenderedMessage(text=f"Entry `{subargs}` not found."))
        return

    await send(
        RenderedMessage(
            text=(
                "Usage:\n"
                "- `!memory` — recent summary\n"
                "- `!memory list [type]` — list entries\n"
                "- `!memory add <type> <title> <content>` — add entry\n"
                "- `!memory search <query>` — search\n"
                "- `!memory delete <id>` — delete entry"
            )
        )
    )


async def handle_branch(
    args: str,
    *,
    project: str | None,
    facade: ProjectMemoryFacade | None,
    send: Any,
) -> None:
    """Manage conversation branches."""
    if not project:
        await send(
            RenderedMessage(text="프로젝트를 먼저 설정하세요. `!project set <name>`")
        )
        return
    if not facade:
        await send(RenderedMessage(text="Branch storage unavailable."))
        return

    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""
    subargs = parts[1].strip() if len(parts) > 1 else ""

    if not subcmd:
        branches = await facade.conv_branches.list(project, status="active")
        if not branches:
            await send(
                RenderedMessage(
                    text=f"프로젝트 `{project}`에 활성 대화 분기가 없습니다."
                )
            )
            return
        lines = [f"**Active branches — {project}**", ""]
        for b in branches:
            git_tag = f" → `{b.git_branch}`" if b.git_branch else ""
            lines.append(f"- `{b.branch_id[:16]}` **{b.label}**{git_tag}")
        await send(RenderedMessage(text="\n".join(lines)))
        return

    if subcmd == "create":
        if not subargs:
            await send(RenderedMessage(text="Usage: `!branch create <label>`"))
            return
        branch = await facade.conv_branches.create(project, subargs)
        await send(
            RenderedMessage(
                text=f"Branch created: `{branch.branch_id[:16]}` **{branch.label}**"
            )
        )
        return

    if subcmd == "list":
        status_filter = subargs.lower() if subargs else None
        valid_statuses = ("active", "merged", "discarded")
        if status_filter and status_filter not in valid_statuses:
            await send(
                RenderedMessage(
                    text=f"Unknown status `{status_filter}`. Available: {', '.join(f'`{s}`' for s in valid_statuses)}"
                )
            )
            return
        branches = await facade.conv_branches.list(project, status=status_filter)
        if not branches:
            label = f" ({status_filter})" if status_filter else ""
            await send(RenderedMessage(text=f"No branches{label} in `{project}`."))
            return
        lines = [f"**Branches — {project}**", ""]
        for b in branches:
            git_tag = f" → `{b.git_branch}`" if b.git_branch else ""
            lines.append(f"- `{b.branch_id[:16]}` [{b.status}] **{b.label}**{git_tag}")
        await send(RenderedMessage(text="\n".join(lines)))
        return

    if subcmd == "merge":
        if not subargs:
            await send(RenderedMessage(text="Usage: `!branch merge <id>`"))
            return
        branch_id, err = await _resolve_id(
            subargs,
            fetch_all=lambda: facade.conv_branches.list(project),
            get_id=lambda b: b.branch_id,
            get_label=lambda b: b.label,
        )
        if err:
            await send(RenderedMessage(text=err))
            return
        assert branch_id is not None
        result = await facade.conv_branches.merge(project, branch_id)
        if result:
            await send(RenderedMessage(text=f"Branch `{result.label}` merged."))
        else:
            await send(RenderedMessage(text=f"Branch `{subargs}` not found."))
        return

    if subcmd == "discard":
        if not subargs:
            await send(RenderedMessage(text="Usage: `!branch discard <id>`"))
            return
        branch_id, err = await _resolve_id(
            subargs,
            fetch_all=lambda: facade.conv_branches.list(project),
            get_id=lambda b: b.branch_id,
            get_label=lambda b: b.label,
        )
        if err:
            await send(RenderedMessage(text=err))
            return
        assert branch_id is not None
        result = await facade.conv_branches.discard(project, branch_id)
        if result:
            await send(RenderedMessage(text=f"Branch `{result.label}` discarded."))
        else:
            await send(RenderedMessage(text=f"Branch `{subargs}` not found."))
        return

    if subcmd == "link-git":
        link_parts = subargs.split(None, 1)
        if len(link_parts) < 2:
            await send(
                RenderedMessage(text="Usage: `!branch link-git <id> <git-branch>`")
            )
            return
        bid_raw, git_branch = link_parts
        branch_id, err = await _resolve_id(
            bid_raw,
            fetch_all=lambda: facade.conv_branches.list(project),
            get_id=lambda b: b.branch_id,
            get_label=lambda b: b.label,
        )
        if err:
            await send(RenderedMessage(text=err))
            return
        assert branch_id is not None
        linked = await facade.conv_branches.link_git_branch(
            project, branch_id, git_branch
        )
        if linked:
            await send(
                RenderedMessage(
                    text=f"Branch `{branch_id[:16]}` linked to `{git_branch}`."
                )
            )
        else:
            await send(RenderedMessage(text=f"Branch `{bid_raw}` not found."))
        return

    await send(
        RenderedMessage(
            text=(
                "Usage:\n"
                "- `!branch` — active branches\n"
                "- `!branch create <label>` — create branch\n"
                "- `!branch list [status]` — list branches\n"
                "- `!branch merge <id>` — merge branch\n"
                "- `!branch discard <id>` — discard branch\n"
                "- `!branch link-git <id> <git-branch>` — link git branch"
            )
        )
    )


async def handle_review(
    args: str,
    *,
    project: str | None,
    facade: ProjectMemoryFacade | None,
    send: Any,
) -> None:
    """Manage review requests."""
    if not project:
        await send(
            RenderedMessage(text="프로젝트를 먼저 설정하세요. `!project set <name>`")
        )
        return
    if not facade:
        await send(RenderedMessage(text="Review storage unavailable."))
        return

    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""
    subargs = parts[1].strip() if len(parts) > 1 else ""

    if not subcmd:
        reviews = await facade.reviews.list(project, status="pending")
        if not reviews:
            await send(
                RenderedMessage(
                    text=f"프로젝트 `{project}`에 대기 중인 리뷰가 없습니다."
                )
            )
            return
        lines = [f"**Pending reviews — {project}**", ""]
        lines.extend(
            f"- `{r.review_id[:16]}` artifact `{r.artifact_id[:16]}` v{r.artifact_version} ({r.created_at})"
            for r in reviews
        )
        await send(RenderedMessage(text="\n".join(lines)))
        return

    if subcmd == "list":
        status_filter = subargs.lower() if subargs else None
        valid_statuses = ("pending", "approved", "rejected")
        if status_filter and status_filter not in valid_statuses:
            await send(
                RenderedMessage(
                    text=f"Unknown status `{status_filter}`. Available: {', '.join(f'`{s}`' for s in valid_statuses)}"
                )
            )
            return
        reviews = await facade.reviews.list(project, status=status_filter)
        if not reviews:
            label = f" ({status_filter})" if status_filter else ""
            await send(RenderedMessage(text=f"No reviews{label} in `{project}`."))
            return
        lines = [f"**Reviews — {project}**", ""]
        for r in reviews:
            lines.append(
                f"- `{r.review_id[:16]}` [{r.status}] artifact `{r.artifact_id[:16]}` v{r.artifact_version}"
            )
        await send(RenderedMessage(text="\n".join(lines)))
        return

    if subcmd == "approve":
        if not subargs:
            await send(RenderedMessage(text="Usage: `!review approve <id> [comment]`"))
            return
        id_and_comment = subargs.split(None, 1)
        rid_raw = id_and_comment[0]
        comment = id_and_comment[1] if len(id_and_comment) > 1 else ""
        review_id, err = await _resolve_id(
            rid_raw,
            fetch_all=lambda: facade.reviews.list(project),
            get_id=lambda r: r.review_id,
            get_label=lambda r: f"artifact {r.artifact_id[:16]}",
        )
        if err:
            await send(RenderedMessage(text=err))
            return
        assert review_id is not None
        result = await facade.reviews.approve(project, review_id, comment=comment)
        if result:
            await send(RenderedMessage(text=f"Review `{review_id[:16]}` approved."))
        else:
            await send(RenderedMessage(text=f"Review `{rid_raw}` not found."))
        return

    if subcmd == "reject":
        if not subargs:
            await send(RenderedMessage(text="Usage: `!review reject <id> [comment]`"))
            return
        id_and_comment = subargs.split(None, 1)
        rid_raw = id_and_comment[0]
        comment = id_and_comment[1] if len(id_and_comment) > 1 else ""
        review_id, err = await _resolve_id(
            rid_raw,
            fetch_all=lambda: facade.reviews.list(project),
            get_id=lambda r: r.review_id,
            get_label=lambda r: f"artifact {r.artifact_id[:16]}",
        )
        if err:
            await send(RenderedMessage(text=err))
            return
        assert review_id is not None
        result = await facade.reviews.reject(project, review_id, comment=comment)
        if result:
            await send(RenderedMessage(text=f"Review `{review_id[:16]}` rejected."))
        else:
            await send(RenderedMessage(text=f"Review `{rid_raw}` not found."))
        return

    await send(
        RenderedMessage(
            text=(
                "Usage:\n"
                "- `!review` — pending reviews\n"
                "- `!review list [status]` — list reviews\n"
                "- `!review approve <id> [comment]` — approve\n"
                "- `!review reject <id> [comment]` — reject"
            )
        )
    )


async def handle_context(
    *,
    project: str | None,
    facade: ProjectMemoryFacade | None,
    send: Any,
) -> None:
    """Show full project context."""
    if not project:
        await send(
            RenderedMessage(text="프로젝트를 먼저 설정하세요. `!project set <name>`")
        )
        return
    if not facade:
        await send(RenderedMessage(text="Context storage unavailable."))
        return

    ctx = await facade.get_project_context(project)
    if not ctx:
        await send(
            RenderedMessage(text=f"프로젝트 `{project}`에 저장된 컨텍스트가 없습니다.")
        )
    else:
        await send(RenderedMessage(text=ctx))


_MIN_PREFIX_LEN = 6


async def _resolve_id[T](
    prefix: str,
    *,
    fetch_all: Any,
    get_id: Any,
    get_label: Any,
) -> tuple[str | None, str | None]:
    """Resolve a full or prefix ID with minimum length and ambiguity handling.

    Returns ``(resolved_id, None)`` on success or ``(None, error_message)`` on failure.
    """
    if len(prefix) < _MIN_PREFIX_LEN:
        return (
            None,
            f"ID prefix too short (minimum {_MIN_PREFIX_LEN} chars): `{prefix}`",
        )

    items = await fetch_all()
    # Exact match first
    for item in items:
        if get_id(item) == prefix:
            return prefix, None
    # Prefix match
    matches = [item for item in items if get_id(item).startswith(prefix)]
    if len(matches) == 1:
        return get_id(matches[0]), None
    if len(matches) == 0:
        return None, f"`{prefix}` not found."
    # Ambiguous — show candidates
    lines = [f"Ambiguous prefix `{prefix}` — {len(matches)} matches:"]
    lines.extend(f"- `{get_id(item)[:16]}` {get_label(item)}" for item in matches[:5])
    if len(matches) > 5:
        lines.append(f"  ... and {len(matches) - 5} more")
    return None, "\n".join(lines)


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
