"""Basic channel/session slash commands for Discord.

Extracted from ``handlers.register_slash_commands`` to keep that file small.
Each command closes over the dependencies passed to
:func:`register_basic_commands`; behaviour is identical to the inline
closures it replaced.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

import discord

if TYPE_CHECKING:
    from .client import DiscordBotClient
    from .prefs import DiscordPrefsStore
    from .state import DiscordStateStore


def register_basic_commands(
    bot: DiscordBotClient,
    *,
    state_store: DiscordStateStore,
    prefs_store: DiscordPrefsStore,
    get_running_task: Callable[..., object],
    cancel_task: Callable[[int], Awaitable[object]],
    require_allowed_user: Callable[..., Awaitable[bool]],
    handle_ctx_command: Callable[..., Awaitable[None]],
) -> None:
    """Register status/bind/unbind/cancel/new/ctx slash commands."""
    pycord_bot = bot.bot

    @pycord_bot.slash_command(
        name="status", description="Show current channel context and status"
    )
    async def status_command(ctx: discord.ApplicationContext) -> None:
        """Show current channel context and running tasks."""
        from .types import DiscordThreadContext

        if ctx.guild is None:
            await ctx.respond(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await require_allowed_user(ctx):
            return

        channel_id = ctx.channel_id
        if (
            channel_id is None
        ):  # pragma: no cover - Pycord guild commands have a channel
            await ctx.respond("This command requires a channel.", ephemeral=True)
            return
        guild_id = ctx.guild.id

        # Get context from state
        context = await state_store.get_context(guild_id, channel_id)

        if context is None:
            await ctx.respond(
                "No context configured for this channel.\n"
                "Use `/bind <project>` to set up this channel.",
                ephemeral=True,
            )
            return

        # Check for running task
        running = get_running_task(channel_id)
        status_line = "idle"
        if running is not None:
            status_line = f"running (message #{running})"

        # Format message based on context type
        if isinstance(context, DiscordThreadContext):
            # Thread context (has specific branch)
            message = (
                f"**Thread Status**\n"
                f"- Project: `{context.project}`\n"
                f"- Branch: `{context.branch}`\n"
                f"- Worktrees dir: `{context.worktrees_dir}`\n"
                f"- Engine: `{context.default_engine}`\n"
                f"- Status: {status_line}"
            )
        else:
            # Channel context (no specific branch, uses worktree_base as default)
            message = (
                f"**Channel Status**\n"
                f"- Project: `{context.project}`\n"
                f"- Default branch: `{context.worktree_base}`\n"
                f"- Worktrees dir: `{context.worktrees_dir}`\n"
                f"- Engine: `{context.default_engine}`\n"
                f"- Status: {status_line}\n\n"
                f"_Use `@branch-name` to create a thread for a specific branch._"
            )
        await ctx.respond(message, ephemeral=True)

    @pycord_bot.slash_command(name="bind", description="Bind this channel to a project")
    async def bind_command(
        ctx: discord.ApplicationContext,
        project: str = cast(
            str,
            discord.Option(description="The project path (e.g., ~/dev/myproject)"),
        ),
        worktrees_dir: str = cast(
            str,
            discord.Option(
                default=".worktrees",
                description="Directory for git worktrees (default: .worktrees)",
            ),
        ),
        default_engine: str = cast(
            str,
            discord.Option(
                default="claude",
                description="Default engine to use (default: claude)",
            ),
        ),
        worktree_base: str = cast(
            str,
            discord.Option(
                default="main",
                description="Base branch for worktrees and default working branch (default: main)",
            ),
        ),
    ) -> None:
        """Bind a channel to a project."""
        if ctx.guild is None:
            await ctx.respond(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await require_allowed_user(ctx):
            return

        channel_id = ctx.channel_id
        if (
            channel_id is None
        ):  # pragma: no cover - Pycord guild commands have a channel
            await ctx.respond("This command requires a channel.", ephemeral=True)
            return
        guild_id = ctx.guild.id

        from .types import DiscordChannelContext

        context = DiscordChannelContext(
            project=project,
            worktrees_dir=worktrees_dir,
            default_engine=default_engine,
            worktree_base=worktree_base,
        )
        await state_store.set_context(guild_id, channel_id, context)

        await ctx.respond(
            f"Bound channel to project `{project}`\n"
            f"- Default branch: `{worktree_base}`\n"
            f"- Worktrees dir: `{worktrees_dir}`\n"
            f"- Engine: `{default_engine}`\n\n"
            f"_Use `@branch-name` to create threads for specific branches._",
            ephemeral=True,
        )

    @pycord_bot.slash_command(
        name="unbind", description="Remove project binding from this channel"
    )
    async def unbind_command(ctx: discord.ApplicationContext) -> None:
        """Unbind a channel from its project."""
        if ctx.guild is None:
            await ctx.respond(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await require_allowed_user(ctx):
            return

        channel_id = ctx.channel_id
        if (
            channel_id is None
        ):  # pragma: no cover - Pycord guild commands have a channel
            await ctx.respond("This command requires a channel.", ephemeral=True)
            return
        guild_id = ctx.guild.id

        await state_store.clear_channel(guild_id, channel_id)
        await prefs_store.clear_channel(guild_id, channel_id)
        await ctx.respond("Channel binding removed.", ephemeral=True)

    @pycord_bot.slash_command(
        name="cancel", description="Cancel the currently running task"
    )
    async def cancel_command(ctx: discord.ApplicationContext) -> None:
        """Cancel a running task."""
        if ctx.guild is None:
            await ctx.respond(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await require_allowed_user(ctx):
            return

        channel_id = ctx.channel_id
        if (
            channel_id is None
        ):  # pragma: no cover - Pycord guild commands have a channel
            await ctx.respond("This command requires a channel.", ephemeral=True)
            return

        running = get_running_task(channel_id)
        if running is None:
            await ctx.respond(
                "No task is currently running in this channel.", ephemeral=True
            )
            return

        await cancel_task(channel_id)
        await ctx.respond("Cancellation requested.", ephemeral=True)

    @pycord_bot.slash_command(
        name="new", description="Clear conversation session for this channel/thread"
    )
    async def new_command(ctx: discord.ApplicationContext) -> None:
        """Clear stored resume tokens to start fresh."""
        if ctx.guild is None:
            await ctx.respond(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await require_allowed_user(ctx):
            return

        channel_id = ctx.channel_id
        if (
            channel_id is None
        ):  # pragma: no cover - Pycord guild commands have a channel
            await ctx.respond("This command requires a channel.", ephemeral=True)
            return
        guild_id = ctx.guild.id

        author_id = getattr(getattr(ctx, "author", None), "id", None)
        if not isinstance(author_id, int):
            author_id = None
        await state_store.clear_sessions(guild_id, channel_id, author_id=author_id)
        await ctx.respond("Session cleared. Starting fresh.", ephemeral=True)

    @pycord_bot.slash_command(name="ctx", description="Show or manage context binding")
    async def ctx_command(
        ctx: discord.ApplicationContext,
        action: str | None = cast(
            str | None,
            discord.Option(
                default=None,
                description="Action to perform (show, clear, or set)",
                choices=["show", "clear", "set"],
            ),
        ),
        project: str | None = cast(
            str | None,
            discord.Option(
                default=None,
                description="Project path (channel only): ~/dev/myproject",
            ),
        ),
        branch: str | None = cast(
            str | None,
            discord.Option(
                default=None,
                description="Branch to bind (use @name). In channels: base branch; in threads: thread branch.",
            ),
        ),
    ) -> None:
        """Show/clear/set context binding."""
        if ctx.guild is None:
            await ctx.respond(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await require_allowed_user(ctx):
            return
        await handle_ctx_command(
            ctx,
            action=action,
            project=project,
            branch=branch,
            state_store=state_store,
        )
