"""Slash command handlers for Mattermost transport.

Mattermost's native slash commands require external integration URLs.
Instead, we detect `/command` prefixes in regular messages and handle
them before passing to the engine dispatcher.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import RunContext
from ..logging import get_logger
from ..transport import RenderedMessage
from .chat_prefs import ChatPrefsStore

logger = get_logger(__name__)


COMMAND_PREFIXES = ("/", "!")


def parse_command(text: str) -> tuple[str | None, str]:
    """Parse a command from message text.

    Recognises ``/command`` and ``!command`` prefixes so that commands
    work on Mattermost mobile where unregistered slash commands are
    blocked.

    Returns (command_name, remaining_args).
    If not a command, returns (None, original_text).
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in COMMAND_PREFIXES:
        return None, text
    parts = stripped.split(None, 1)
    cmd = parts[0][1:].lower()  # strip leading prefix char
    args = parts[1] if len(parts) > 1 else ""
    return cmd, args


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
        "| `!model <engine>` | Switch default engine |",
        "| `!trigger <all\\|mentions>` | Set trigger mode |",
        "| `!project list\\|set\\|info` | Manage project binding |",
        "| `!persona add\\|list\\|remove` | Manage personas |",
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
    """Switch the default engine for this channel."""
    engine = args.strip().lower()
    available = list(runtime.available_engine_ids())

    if not engine:
        current = None
        if chat_prefs:
            current = await chat_prefs.get_default_engine(channel_id)
        current_display = current or runtime.default_engine
        engine_list = ", ".join(f"`{e}`" for e in available)
        await send(
            RenderedMessage(
                text=f"Current engine: `{current_display}`\nAvailable: {engine_list}\n\nUsage: `/model <engine>`"
            )
        )
        return

    engine_map = {e.lower(): e for e in available}
    if engine not in engine_map:
        await send(
            RenderedMessage(
                text=f"Unknown engine `{engine}`. Available: {', '.join(f'`{e}`' for e in available)}"
            )
        )
        return

    if chat_prefs:
        await chat_prefs.set_default_engine(channel_id, engine_map[engine])

    await send(RenderedMessage(text=f"Default engine set to `{engine_map[engine]}`"))
    logger.info("command.model", channel_id=channel_id, engine=engine_map[engine])


async def handle_trigger(
    args: str,
    *,
    channel_id: str,
    chat_prefs: ChatPrefsStore | None,
    send: Any,
) -> None:
    """Set trigger mode for this channel."""
    mode = args.strip().lower()

    if mode not in ("all", "mentions"):
        current = "all"
        if chat_prefs:
            current = await chat_prefs.get_trigger_mode(channel_id) or "all"
        await send(
            RenderedMessage(
                text=f"Current trigger mode: `{current}`\n\nUsage: `/trigger all` or `/trigger mentions`"
            )
        )
        return

    if chat_prefs:
        await chat_prefs.set_trigger_mode(channel_id, mode)

    desc = (
        "respond to all messages" if mode == "all" else "respond only when @mentioned"
    )
    await send(RenderedMessage(text=f"Trigger mode set to `{mode}` — {desc}"))
    logger.info("command.trigger", channel_id=channel_id, mode=mode)


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
    engine = runtime.default_engine
    trigger = "all"
    project_display = "none"
    if chat_prefs:
        engine = await chat_prefs.get_default_engine(channel_id) or engine
        trigger = await chat_prefs.get_trigger_mode(channel_id) or "all"
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
        f"- Session: {'active' if has_session else 'none'}",
        f"- Channel: `{channel_id}`",
    ]
    await send(RenderedMessage(text="\n".join(lines)))


async def handle_project(
    args: str,
    *,
    channel_id: str,
    runtime: Any,
    chat_prefs: ChatPrefsStore | None,
    projects_root: str | None,
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
                    and d.name not in {c.lower() for c in configured}
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

        # Check discovered projects in projects_root
        if project_key is None and projects_root:
            root = Path(projects_root).expanduser()
            candidate = root / name
            if candidate.is_dir() and (candidate / ".git").exists():
                project_key = name

        if project_key is None:
            await send(
                RenderedMessage(
                    text=f"Unknown project `{name}`. Use `!project list` to see available projects."
                )
            )
            return

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


async def handle_cancel(
    *,
    channel_id: str,
    running_tasks: dict,
    send: Any,
) -> None:
    """Cancel the running task in this channel."""
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
