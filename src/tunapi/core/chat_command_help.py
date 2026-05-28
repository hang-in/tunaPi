from __future__ import annotations

from typing import Any

from ..transport import RenderedMessage


async def handle_help_command(
    *,
    runtime: Any,
    send: Any,
    title: str = "**tunapi commands**",
    subtitle: str | None = None,
    commands_table: list[str],
    engines_label: str = "**Engines:**",
    projects_label: str | None = "**Projects:**",
    footer: str | None = None,
) -> None:
    engines = list(runtime.available_engine_ids())
    projects = sorted(set(runtime.project_aliases()), key=str.lower)

    lines = [title]
    if subtitle:
        lines.extend(["", subtitle])
    lines.extend([""] + commands_table + [""])
    lines.append(f"{engines_label} {', '.join(f'`{e}`' for e in engines) or 'none'}")
    lines.append("")
    if projects_label:
        lines.append(
            f"{projects_label} {', '.join(f'`{p}`' for p in projects) or 'none'}"
        )
        lines.append("")
    if footer:
        lines.append(footer)

    await send(RenderedMessage(text="\n".join(lines)))
