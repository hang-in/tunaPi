from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .state import DiscordStateStore


def _is_admin(ctx: discord.ApplicationContext) -> bool:
    """Check if the user has admin permissions in the guild."""
    if ctx.guild is None:
        return False
    member = ctx.author
    if isinstance(member, discord.Member):
        return member.guild_permissions.administrator
    return False


async def _require_admin(ctx: discord.ApplicationContext) -> bool:
    """Check admin permission and respond with error if not admin."""
    if not _is_admin(ctx):
        await ctx.respond(
            "This command requires administrator permissions.",
            ephemeral=True,
        )
        return False
    return True


def _normalize_branch_name(value: str) -> str:
    branch = value.strip()
    if branch.startswith("@"):
        branch = branch[1:]
    return branch.strip()


async def _handle_ctx_command(
    ctx: discord.ApplicationContext,
    *,
    action: str | None,
    project: str | None,
    branch: str | None,
    state_store: DiscordStateStore,
    require_admin: Callable[[discord.ApplicationContext], Awaitable[bool]]
    | None = None,
) -> None:
    """Handle /ctx show|clear|set for channel/thread context."""
    from .types import DiscordChannelContext, DiscordThreadContext

    require_admin = require_admin or _require_admin

    if ctx.guild is None:
        await ctx.respond("This command can only be used in a server.", ephemeral=True)
        return

    guild_id = ctx.guild.id
    channel_id = ctx.channel_id
    thread_id = None

    if isinstance(ctx.channel, discord.Thread):
        thread_id = ctx.channel_id
        channel_id = ctx.channel.parent_id or ctx.channel_id

    channel_context: DiscordChannelContext | None = None
    thread_context: DiscordThreadContext | None = None

    if thread_id is not None:
        ctx_data = await state_store.get_context(guild_id, thread_id)
        if isinstance(ctx_data, DiscordThreadContext):
            thread_context = ctx_data

    ctx_data = await state_store.get_context(guild_id, channel_id)
    if isinstance(ctx_data, DiscordChannelContext):
        channel_context = ctx_data

    if action is None:
        action = "show"

    if action == "clear":
        if not await require_admin(ctx):
            return
        target_id = thread_id if thread_id is not None else channel_id
        await state_store.set_context(guild_id, target_id, None)
        cleared = "thread" if thread_id is not None else "channel"
        await ctx.respond(f"{cleared.title()} context binding cleared.", ephemeral=True)
        return

    if action == "set":
        if not await require_admin(ctx):
            return

        if branch is not None and not branch.strip():
            branch = None
        normalized_branch = (
            _normalize_branch_name(branch) if branch is not None else None
        )

        if thread_id is not None:
            if project is not None:
                await ctx.respond(
                    "In threads, `/ctx set` only supports rebinding the branch.\n"
                    "Use `/ctx set <project> [@base-branch]` in the parent channel to change the project.",
                    ephemeral=True,
                )
                return
            if normalized_branch is None:
                await ctx.respond(
                    "Usage (in a thread): `/ctx set @branch-name`", ephemeral=True
                )
                return

            base = thread_context or channel_context
            if base is None:
                await ctx.respond(
                    "No project is bound for this thread/channel.\n"
                    "Use `/bind <project>` (or `/ctx set <project>`) in the parent channel first.",
                    ephemeral=True,
                )
                return

            new_thread_context = DiscordThreadContext(
                project=base.project,
                branch=normalized_branch,
                worktrees_dir=base.worktrees_dir,
                default_engine=base.default_engine,
            )
            await state_store.set_context(guild_id, thread_id, new_thread_context)
            await ctx.respond(
                "Thread context updated.\n"
                f"- Project: `{new_thread_context.project}`\n"
                f"- Branch: `{new_thread_context.branch}`",
                ephemeral=True,
            )
            return

        if project is None and channel_context is None:
            await ctx.respond(
                "No context is bound to this channel.\n"
                "Usage: `/ctx set <project> [@base-branch]`",
                ephemeral=True,
            )
            return

        if project is None:
            project = channel_context.project if channel_context is not None else None

        if project is None:
            await ctx.respond(
                "Usage: `/ctx set <project> [@base-branch]`", ephemeral=True
            )
            return

        worktrees_dir = (
            channel_context.worktrees_dir if channel_context else ".worktrees"
        )
        default_engine = channel_context.default_engine if channel_context else "claude"
        worktree_base = (
            normalized_branch
            if normalized_branch is not None
            else (channel_context.worktree_base if channel_context else "main")
        )

        new_channel_context = DiscordChannelContext(
            project=project,
            worktrees_dir=worktrees_dir,
            default_engine=default_engine,
            worktree_base=worktree_base,
        )
        await state_store.set_context(guild_id, channel_id, new_channel_context)
        await ctx.respond(
            "Channel context updated.\n"
            f"- Project: `{new_channel_context.project}`\n"
            f"- Base branch: `{new_channel_context.worktree_base}`",
            ephemeral=True,
        )
        return

    resolved_project: str | None = None
    resolved_branch: str | None = None
    resolved_engine: str | None = None
    resolved_source: str | None = None

    if thread_context is not None:
        resolved_project = thread_context.project
        resolved_branch = thread_context.branch
        resolved_engine = thread_context.default_engine
        resolved_source = "thread"
    elif channel_context is not None:
        resolved_project = channel_context.project
        resolved_branch = channel_context.worktree_base
        resolved_engine = channel_context.default_engine
        resolved_source = "channel"

    if resolved_project is None or resolved_branch is None or resolved_engine is None:
        await ctx.respond(
            "No context bound to this channel/thread.\n"
            "Use `/bind <project>` to set up this channel.",
            ephemeral=True,
        )
        return

    lines = [
        "**Context**",
        "**Resolved**",
        f"- Project: `{resolved_project}`",
        f"- Branch: `{resolved_branch}`",
        f"- Engine: `{resolved_engine}`",
        f"- Source: {resolved_source}",
        "",
        "**Bound**",
    ]

    if channel_context is not None:
        lines.append(
            f"- Channel: `{channel_context.project}` @ `{channel_context.worktree_base}`"
        )
        lines.append(f"  - Engine: `{channel_context.default_engine}`")
        lines.append(f"  - Worktrees dir: `{channel_context.worktrees_dir}`")
    else:
        lines.append("- Channel: _none_")

    if thread_id is not None:
        if thread_context is not None:
            lines.append(
                f"- Thread: `{thread_context.project}` @ `{thread_context.branch}`"
            )
            lines.append(f"  - Engine: `{thread_context.default_engine}`")
            lines.append(f"  - Worktrees dir: `{thread_context.worktrees_dir}`")
        else:
            lines.append("- Thread: _none_")

    await ctx.respond("\n".join(lines), ephemeral=True)
