from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import HOME_CONFIG_PATH, ConfigError, read_config, write_config
from ..context import RunContext
from ..transport import RenderedMessage


def _register_project_in_config(
    name: str,
    path: Path,
    channel_id: str,
    *,
    runtime: Any,
    config_path: Path | None,
) -> None:
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


async def handle_project_command(
    args: str,
    *,
    channel_id: str,
    runtime: Any,
    chat_prefs: Any | None,
    projects_root: str | None,
    config_path: Path | None = None,
    send: Any,
    title_projects: str = "**Projects**",
    title_channel_project: str = "**Channel project:**",
    title_branch: str = "**Branch:**",
    title_no_bound: str = "No project bound to this channel.",
    success_msg_fmt: Callable[
        [str], str
    ] = lambda pkey: f"Project set to `{pkey}` for this channel.",
    set_context_filter: Callable[[str], bool] = lambda cid: True,
    logger_cb: Callable[[str, str], None] | None = None,
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

        lines = [title_projects, ""]
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
                name,
                discovered_path,
                channel_id,
                runtime=runtime,
                config_path=config_path,
            )

        if chat_prefs and set_context_filter(channel_id):
            await chat_prefs.set_context(channel_id, RunContext(project=project_key))
        await send(RenderedMessage(text=success_msg_fmt(project_key)))
        if logger_cb:
            logger_cb(channel_id, project_key)
        return

    if subcmd == "info":
        ctx = None
        if chat_prefs:
            ctx = await chat_prefs.get_context(channel_id)

        if ctx and ctx.project:
            lines = [
                f"{title_channel_project} `{ctx.project}`",
            ]
            if ctx.branch:
                lines.append(f"{title_branch} `{ctx.branch}`")
        else:
            lines = [
                title_no_bound,
                "",
                "Usage: `!project set <name>`",
            ]
        await send(RenderedMessage(text="\n".join(lines)))
        return

    await send(
        RenderedMessage(
            text="Usage: `!project list` | `!project set <name>` | `!project info`"
        )
    )


async def handle_persona_command(
    args: str,
    *,
    chat_prefs: Any | None,
    send: Any,
    title_personas: str = "**Personas**",
    fmt_item: Callable[[str, str], str] = lambda name,
    display: f"- **{name}**: {display}",
    fmt_title: Callable[[str], str] = lambda name: f"**{name}**",
    logger_add_cb: Callable[[str], None] | None = None,
    logger_remove_cb: Callable[[str], None] | None = None,
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
        if logger_add_cb:
            logger_add_cb(name)
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
        lines = [title_personas, ""]
        for name, p in sorted(personas.items()):
            display = p.prompt if len(p.prompt) <= 80 else p.prompt[:77] + "..."
            lines.append(fmt_item(name, display))
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
            if logger_remove_cb:
                logger_remove_cb(name)
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
            await send(
                RenderedMessage(text=f"{fmt_title(persona.name)}\n\n{persona.prompt}")
            )
        else:
            await send(RenderedMessage(text=f"Persona `{name}` not found."))
        return

    await send(
        RenderedMessage(
            text='Usage: `!persona add <name> "<prompt>"` | `!persona list` | `!persona show <name>` | `!persona remove <name>`'
        )
    )
