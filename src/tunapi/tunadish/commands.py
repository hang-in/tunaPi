"""tunadish command handlers — ! 커맨드 + JSON-RPC 메서드 공용.

Slack/Mattermost handler를 직접 import하지 않고 core 모듈만 사용.
각 handler의 send 콜백은 RenderedMessage를 받아 클라이언트에 전달.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..core.commands import parse_command  # noqa: F401 — re-exported
from ..engine_models import get_models, shorten_model
from ..transport import RenderedMessage
from ..context import RunContext
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
    engines = list(runtime.available_engine_ids())
    projects = sorted(set(runtime.project_aliases()), key=str.lower)

    lines = [
        "**tunapi commands**",
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
        '| `!rt "topic"` | Multi-agent roundtable |',
        "| `!status` | Show current session info |",
        "| `!cancel` | Cancel running task |",
        "",
        f"**Engines:** {', '.join(f'`{e}`' for e in engines) or 'none'}",
        "",
        f"**Projects:** {', '.join(f'`{p}`' for p in projects) or 'none'}",
    ]
    await send(RenderedMessage(text="\n".join(lines)))


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
    parts = args.strip().split(None, 1)
    available = list(runtime.available_engine_ids())
    engine_map = {e.lower(): e for e in available}

    if not parts or not parts[0]:
        current_engine = None
        if chat_prefs:
            current_engine = await chat_prefs.get_default_engine(channel_id)
        current_display = current_engine or runtime.default_engine
        model_display = ""
        if chat_prefs:
            model = await chat_prefs.get_engine_model(channel_id, current_display)
            if model:
                model_display = f"\nModel: `{model}` ({shorten_model(model)})"
        engine_list = ", ".join(f"`{e}`" for e in available)
        await send(
            RenderedMessage(
                text=(
                    f"Current engine: `{current_display}`{model_display}\n"
                    f"Available: {engine_list}\n\n"
                    "Usage: `!model <engine>` | `!model <engine> <model>` | `!model <engine> clear`"
                )
            )
        )
        return

    first = parts[0].lower()
    second = parts[1].strip() if len(parts) > 1 else ""

    if first not in engine_map:
        await send(
            RenderedMessage(
                text=f"Unknown engine `{first}`. Available: {', '.join(f'`{e}`' for e in available)}"
            )
        )
        return

    canonical_engine = engine_map[first]

    if not second:
        if chat_prefs:
            await chat_prefs.set_default_engine(channel_id, canonical_engine)
        model_display = ""
        if chat_prefs:
            model = await chat_prefs.get_engine_model(channel_id, canonical_engine)
            if model:
                model_display = f" (model: `{model}`)"
        await send(
            RenderedMessage(text=f"Default engine set to `{canonical_engine}`{model_display}")
        )
        return

    if second.lower() == "clear":
        if chat_prefs:
            await chat_prefs.clear_engine_model(channel_id, canonical_engine)
        await send(RenderedMessage(text=f"Model override cleared for `{canonical_engine}`"))
        return

    if chat_prefs:
        await chat_prefs.set_engine_model(channel_id, canonical_engine, second)
    await send(
        RenderedMessage(text=f"Model for `{canonical_engine}` set to `{second}` ({shorten_model(second)})")
    )


async def handle_models(
    args: str,
    *,
    channel_id: str,
    runtime: TransportRuntime,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
    available = list(runtime.available_engine_ids())
    target = args.strip().lower() if args.strip() else None

    if target:
        engine_map = {e.lower(): e for e in available}
        if target not in engine_map:
            await send(
                RenderedMessage(
                    text=f"Unknown engine `{target}`. Available: {', '.join(f'`{e}`' for e in available)}"
                )
            )
            return
        engines_to_show = [engine_map[target]]
    else:
        engines_to_show = available

    lines: list[str] = ["**Available Models**", ""]
    current_models: dict[str, str] = {}
    if chat_prefs:
        current_models = await chat_prefs.get_all_engine_models(channel_id)

    for engine in engines_to_show:
        models, source = get_models(engine)
        current = current_models.get(engine)
        current_marker = f" ← current: `{current}`" if current else ""

        if models:
            model_list = ", ".join(f"`{m}`" for m in models)
            lines.append(f"**{engine}** ({source}){current_marker}")
            lines.append(f"  {model_list}")
        else:
            lines.append(f"**{engine}** (no known models){current_marker}")
        lines.append("")

    lines.append("Set: `!model <engine> <model>` | Clear: `!model <engine> clear`")
    await send(RenderedMessage(text="\n".join(lines)))


# ---------------------------------------------------------------------------
# !trigger
# ---------------------------------------------------------------------------

async def handle_trigger(
    args: str,
    *,
    channel_id: str,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
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

    if chat_prefs:
        await chat_prefs.set_trigger_mode(channel_id, mode)

    desc = "respond to all messages" if mode == "all" else "respond only when @mentioned"
    await send(RenderedMessage(text=f"Trigger mode set to `{mode}` — {desc}"))


# ---------------------------------------------------------------------------
# !status
# ---------------------------------------------------------------------------

async def handle_status(
    *,
    channel_id: str,
    runtime: TransportRuntime,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
    engine = runtime.default_engine
    trigger = "mentions"
    project_display = "none"
    if chat_prefs:
        engine = await chat_prefs.get_default_engine(channel_id) or engine
        trigger = await chat_prefs.get_trigger_mode(channel_id) or "mentions"
        ctx = await chat_prefs.get_context(channel_id)
        if ctx and ctx.project:
            project_display = f"`{ctx.project}`"
            if ctx.branch:
                project_display += f" ({ctx.branch})"

    lines = [
        "**Session status**",
        "",
        f"- Engine: `{engine}`",
        f"- Project: {project_display}",
        f"- Trigger: `{trigger}`",
        f"- Channel: `{channel_id}`",
    ]
    await send(RenderedMessage(text="\n".join(lines)))


# ---------------------------------------------------------------------------
# !project
# ---------------------------------------------------------------------------

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
    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""
    subargs = parts[1].strip() if len(parts) > 1 else ""

    if subcmd == "list":
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
        project_key = runtime.normalize_project_key(name)
        discovered_path: Path | None = None

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

        if discovered_path is not None:
            _register_project_in_config(
                name, discovered_path, channel_id, runtime=runtime, config_path=config_path
            )

        # context_store가 source of truth (__rpc__ 가상 채널은 제외)
        if context_store and channel_id != "__rpc__":
            await context_store.set_context(channel_id, RunContext(project=project_key))
        await send(RenderedMessage(text=f"Project set to `{project_key}`."))
        return

    if subcmd == "info":
        ctx = None
        if context_store:
            ctx = await context_store.get_context(channel_id)
        if ctx and ctx.project:
            lines = [f"**Channel project:** `{ctx.project}`"]
            if ctx.branch:
                lines.append(f"**Branch:** `{ctx.branch}`")
        else:
            lines = ["No project bound.", "", "Usage: `!project set <name>`"]
        await send(RenderedMessage(text="\n".join(lines)))
        return

    await send(
        RenderedMessage(text="Usage: `!project list` | `!project set <name>` | `!project info`")
    )


def _register_project_in_config(
    name: str,
    path: Path,
    channel_id: str,
    *,
    runtime: Any,
    config_path: Path | None,
) -> None:
    try:
        from ..config import HOME_CONFIG_PATH, read_config, write_config
        cfg_path = config_path or HOME_CONFIG_PATH
        config = read_config(cfg_path)
        projects = config.setdefault("projects", {})
        if name not in projects:
            projects[name] = {"path": str(path.resolve())}
            write_config(config, cfg_path)
    except Exception:
        pass
    try:
        runtime._projects.register_discovered(name, path.resolve(), channel_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# !persona
# ---------------------------------------------------------------------------

async def handle_persona(
    args: str,
    *,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
    if not chat_prefs:
        await send(RenderedMessage(text="Persona storage unavailable."))
        return

    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""
    subargs = parts[1].strip() if len(parts) > 1 else ""

    if subcmd == "add":
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
        return

    if subcmd == "list":
        personas = await chat_prefs.list_personas()
        if not personas:
            await send(RenderedMessage(text='No personas defined. Use `!persona add <name> "<prompt>"`'))
            return
        lines = ["**Personas**", ""]
        for name, p in sorted(personas.items()):
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

    await send(
        RenderedMessage(
            text='Usage: `!persona add <name> "<prompt>"` | `!persona list` | `!persona show <name>` | `!persona remove <name>`'
        )
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
    if not project:
        await send(RenderedMessage(text="프로젝트를 먼저 설정하세요. `!project set <name>`"))
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
            await send(RenderedMessage(text=f"프로젝트 `{project}`에 저장된 메모리가 없습니다."))
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
                RenderedMessage(text="Usage: `!memory add <type> <title> <content>`\nTypes: `decision`, `review`, `idea`, `context`")
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
            type=entry_type_raw,
            title=title,
            content=content,
            source=source,
        )
        await send(RenderedMessage(text=f"Entry added: `{entry.id[:16]}` [{entry.type}] **{entry.title}** (source: {source})"))
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
    if not project:
        await send(RenderedMessage(text="프로젝트를 먼저 설정하세요. `!project set <name>`"))
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
            await send(RenderedMessage(text=f"프로젝트 `{project}`에 활성 대화 분기가 없습니다."))
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
        await send(RenderedMessage(text=f"Branch created: `{branch.branch_id[:16]}` **{branch.label}**"))
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

    await send(
        RenderedMessage(
            text=(
                "Usage:\n"
                "- `!branch` — active branches\n"
                "- `!branch create <label>` — create branch\n"
                "- `!branch list [status]` — list branches\n"
                "- `!branch merge <id>` — merge branch\n"
                "- `!branch discard <id>` — discard branch"
            )
        )
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
    if not project:
        await send(RenderedMessage(text="프로젝트를 먼저 설정하세요. `!project set <name>`"))
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
            await send(RenderedMessage(text=f"프로젝트 `{project}`에 대기 중인 리뷰가 없습니다."))
            return
        lines = [f"**Pending reviews — {project}**", ""]
        for r in reviews:
            lines.append(f"- `{r.review_id[:16]}` artifact `{r.artifact_id[:16]}` v{r.artifact_version}")
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
            lines.append(f"- `{r.review_id[:16]}` [{r.status}] artifact `{r.artifact_id[:16]}` v{r.artifact_version}")
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


# ---------------------------------------------------------------------------
# !context
# ---------------------------------------------------------------------------

async def handle_context(
    *,
    project: str | None,
    facade: ProjectMemoryFacade | None,
    send: Any,
) -> None:
    if not project:
        await send(RenderedMessage(text="프로젝트를 먼저 설정하세요. `!project set <name>`"))
        return
    if not facade:
        await send(RenderedMessage(text="Context storage unavailable."))
        return

    ctx = await facade.get_project_context(project)
    if not ctx:
        await send(RenderedMessage(text=f"프로젝트 `{project}`에 저장된 컨텍스트가 없습니다."))
    else:
        await send(RenderedMessage(text=ctx))


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
# ID resolution helper
# ---------------------------------------------------------------------------

_MIN_PREFIX_LEN = 6


async def _resolve_id(
    prefix: str,
    *,
    fetch_all: Any,
    get_id: Any,
    get_label: Any,
) -> tuple[str | None, str | None]:
    if len(prefix) < _MIN_PREFIX_LEN:
        return None, f"ID prefix too short (minimum {_MIN_PREFIX_LEN} chars): `{prefix}`"

    items = await fetch_all()
    for item in items:
        if get_id(item) == prefix:
            return prefix, None
    matches = [item for item in items if get_id(item).startswith(prefix)]
    if len(matches) == 1:
        return get_id(matches[0]), None
    if len(matches) == 0:
        return None, f"`{prefix}` not found."
    lines = [f"Ambiguous prefix `{prefix}` — {len(matches)} matches:"]
    for item in matches[:5]:
        lines.append(f"- `{get_id(item)[:16]}` {get_label(item)}")
    if len(matches) > 5:
        lines.append(f"  ... and {len(matches) - 5} more")
    return None, "\n".join(lines)


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
                args, channel_id=channel_id, runtime=runtime,
                chat_prefs=chat_prefs, send=send,
            )
        case "models":
            await handle_models(
                args, channel_id=channel_id, runtime=runtime,
                chat_prefs=chat_prefs, send=send,
            )
        case "trigger":
            await handle_trigger(
                args, channel_id=channel_id, chat_prefs=chat_prefs, send=send,
            )
        case "project":
            await handle_project(
                args, channel_id=channel_id, runtime=runtime,
                chat_prefs=chat_prefs, context_store=context_store,
                projects_root=projects_root,
                config_path=config_path, send=send,
            )
        case "persona":
            await handle_persona(args, chat_prefs=chat_prefs, send=send)
        case "memory":
            project = await _get_project()
            engine = await _get_engine()
            await handle_memory(
                args, project=project, facade=facade,
                current_engine=engine or runtime.default_engine, send=send,
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
                channel_id=channel_id, runtime=runtime,
                chat_prefs=chat_prefs, send=send,
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
