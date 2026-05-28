from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..transport import RenderedMessage

_MIN_PREFIX_LEN = 6


async def _resolve_id[T](
    prefix: str,
    *,
    fetch_all: Any,
    get_id: Any,
    get_label: Any,
) -> tuple[str | None, str | None]:
    """Resolve a full or prefix ID with minimum length and ambiguity handling."""
    if len(prefix) < _MIN_PREFIX_LEN:
        return (
            None,
            f"ID prefix too short (minimum {_MIN_PREFIX_LEN} chars): `{prefix}`",
        )

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
    lines.extend(f"- `{get_id(item)[:16]}` {get_label(item)}" for item in matches[:5])
    if len(matches) > 5:
        lines.append(f"  ... and {len(matches) - 5} more")
    return None, "\n".join(lines)


async def handle_memory_command(
    args: str,
    *,
    project: str | None,
    facade: Any | None,
    current_engine: str | None = None,
    send: Any,
    title_memory_fmt: Callable[[str], str] = lambda proj: f"**Memory — {proj}**",
    title_search_fmt: Callable[
        [str], str
    ] = lambda query: f"**Search results — {query}**",
    fmt_item: Callable[[Any, str, str], str] = lambda e,
    tag_str,
    ts: f"- `{e.id[:16]}` [{e.type}] **{e.title}**{tag_str} ({ts})",
    fmt_search_item: Callable[
        [Any], str
    ] = lambda e: f"- `{e.id[:16]}` [{e.type}] **{e.title}**",
) -> None:
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
        lines = [title_memory_fmt(project), ""]
        for e in entries:
            ts = time.strftime("%m/%d", time.localtime(e.timestamp))
            tag_str = f" [{', '.join(e.tags)}]" if e.tags else ""
            lines.append(fmt_item(e, tag_str, ts))
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
        bold_title = fmt_search_item(entry).split("] ")[-1]
        await send(
            RenderedMessage(
                text=f"Entry added: `{entry.id[:16]}` [{entry.type}] {bold_title} (source: {source})"
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
        lines = [title_search_fmt(subargs), ""]
        for e in results[:10]:
            lines.append(fmt_search_item(e))
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


async def handle_branch_command(
    args: str,
    *,
    project: str | None,
    facade: Any | None,
    send: Any,
    title_active_fmt: Callable[
        [str], str
    ] = lambda proj: f"**Active branches — {proj}**",
    title_branches_fmt: Callable[[str], str] = lambda proj: f"**Branches — {proj}**",
    fmt_active_item: Callable[[Any, str], str] = lambda b,
    git_tag: f"- `{b.branch_id[:16]}` **{b.label}**{git_tag}",
    fmt_branch_item: Callable[[Any, str], str] = lambda b,
    git_tag: f"- `{b.branch_id[:16]}` [{b.status}] **{b.label}**{git_tag}",
    fmt_create_msg: Callable[
        [Any], str
    ] = lambda b: f"Branch created: `{b.branch_id[:16]}` **{b.label}**",
    fmt_bold: Callable[[str], str] = lambda text: f"**{text}**",
) -> None:
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
        lines = [title_active_fmt(project), ""]
        for b in branches:
            git_tag = f" → `{b.git_branch}`" if b.git_branch else ""
            lines.append(fmt_active_item(b, git_tag))
        await send(RenderedMessage(text="\n".join(lines)))
        return

    if subcmd == "create":
        if not subargs:
            await send(RenderedMessage(text="Usage: `!branch create <label>`"))
            return
        branch = await facade.conv_branches.create(project, subargs)
        await send(RenderedMessage(text=fmt_create_msg(branch)))
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
        lines = [title_branches_fmt(project), ""]
        for b in branches:
            git_tag = f" → `{b.git_branch}`" if b.git_branch else ""
            lines.append(fmt_branch_item(b, git_tag))
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


async def handle_review_command(
    args: str,
    *,
    project: str | None,
    facade: Any | None,
    send: Any,
    title_pending_fmt: Callable[
        [str], str
    ] = lambda proj: f"**Pending reviews — {proj}**",
    title_reviews_fmt: Callable[[str], str] = lambda proj: f"**Reviews — {proj}**",
    fmt_pending_item: Callable[
        [Any], str
    ] = lambda r: f"- `{r.review_id[:16]}` artifact `{r.artifact_id[:16]}` v{r.artifact_version} ({r.created_at})",
    fmt_review_item: Callable[
        [Any], str
    ] = lambda r: f"- `{r.review_id[:16]}` [{r.status}] artifact `{r.artifact_id[:16]}` v{r.artifact_version}",
) -> None:
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
        lines = [title_pending_fmt(project), ""]
        lines.extend(fmt_pending_item(r) for r in reviews)
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
        lines = [title_reviews_fmt(project), ""]
        lines.extend(fmt_review_item(r) for r in reviews)
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


async def handle_context_command(
    *,
    project: str | None,
    facade: Any | None,
    send: Any,
) -> None:
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
