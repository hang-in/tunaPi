"""Slash command and message handlers for Discord."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

import discord

from .allowlist import is_user_allowed
from .overrides import (
    REASONING_LEVELS,
    is_valid_reasoning_level,
    resolve_effective_default_engine,
    resolve_overrides,
    resolve_trigger_mode,
    supports_reasoning,
)

if TYPE_CHECKING:
    from tunapi.runner_bridge import RunningTasks
    from tunapi.transport_runtime import TransportRuntime

    from .bridge import DiscordBridgeConfig, DiscordFilesSettings
    from .client import DiscordBotClient
    from .prefs import DiscordPrefsStore
    from .state import DiscordStateStore
    from .voice import VoiceManager


def _is_admin(ctx: discord.ApplicationContext) -> bool:
    """Check if the user has admin permissions in the guild."""
    if ctx.guild is None:
        return False
    member = ctx.author
    if isinstance(member, discord.Member):
        return member.guild_permissions.administrator
    return False


async def _require_admin(ctx: discord.ApplicationContext) -> bool:
    """Check admin permission and respond with error if not admin.

    Returns True if admin check passed, False if not (and error was sent).
    """
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
) -> None:
    """Handle /ctx show|clear|set for channel/thread context."""
    from .types import DiscordChannelContext, DiscordThreadContext

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
        if not await _require_admin(ctx):
            return
        target_id = thread_id if thread_id is not None else channel_id
        await state_store.set_context(guild_id, target_id, None)
        cleared = "thread" if thread_id is not None else "channel"
        await ctx.respond(f"{cleared.title()} context binding cleared.", ephemeral=True)
        return

    if action == "set":
        if not await _require_admin(ctx):
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

        # Channel update.
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

    # Show context (report bound + resolved)
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


def register_slash_commands(
    bot: DiscordBotClient,
    *,
    state_store: DiscordStateStore,
    prefs_store: DiscordPrefsStore,
    get_running_task: callable,
    cancel_task: callable,
    allowed_user_ids: frozenset[int] | None = None,
    trigger_mode_default: Literal["all", "mentions"] = "all",
    runtime: TransportRuntime | None = None,
    files: DiscordFilesSettings | None = None,
    voice_manager: VoiceManager | None = None,
) -> None:
    """Register slash commands with the bot."""
    pycord_bot = bot.bot

    async def require_allowed_user(
        ctx: discord.ApplicationContext, *, require_files: bool = False
    ) -> bool:
        user_id = getattr(getattr(ctx, "author", None), "id", None)
        if not isinstance(user_id, int):
            user_id = None

        if not is_user_allowed(allowed_user_ids, user_id):
            await ctx.respond("You are not allowed to use this bot.", ephemeral=True)
            return False

        if (
            require_files
            and files is not None
            and not is_user_allowed(files.allowed_user_ids, user_id)
        ):
            await ctx.respond(
                "You are not allowed to use file transfers.",
                ephemeral=True,
            )
            return False

        return True

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
        project: str = discord.Option(
            description="The project path (e.g., ~/dev/myproject)"
        ),
        worktrees_dir: str = discord.Option(
            default=".worktrees",
            description="Directory for git worktrees (default: .worktrees)",
        ),
        default_engine: str = discord.Option(
            default="claude",
            description="Default engine to use (default: claude)",
        ),
        worktree_base: str = discord.Option(
            default="main",
            description="Base branch for worktrees and default working branch (default: main)",
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
        guild_id = ctx.guild.id

        author_id = getattr(getattr(ctx, "author", None), "id", None)
        if not isinstance(author_id, int):
            author_id = None
        await state_store.clear_sessions(guild_id, channel_id, author_id=author_id)
        await ctx.respond("Session cleared. Starting fresh.", ephemeral=True)

    @pycord_bot.slash_command(name="ctx", description="Show or manage context binding")
    async def ctx_command(
        ctx: discord.ApplicationContext,
        action: str | None = discord.Option(
            default=None,
            description="Action to perform (show, clear, or set)",
            choices=["show", "clear", "set"],
        ),
        project: str | None = discord.Option(
            default=None,
            description="Project path (channel only): ~/dev/myproject",
        ),
        branch: str | None = discord.Option(
            default=None,
            description="Branch to bind (use @name). In channels: base branch; in threads: thread branch.",
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
        await _handle_ctx_command(
            ctx,
            action=action,
            project=project,
            branch=branch,
            state_store=state_store,
        )

    @pycord_bot.slash_command(name="agent", description="Show or manage default agent")
    async def agent_command(
        ctx: discord.ApplicationContext,
        action: str | None = discord.Option(
            default=None,
            description="Action to perform (show, set, clear)",
            choices=["show", "set", "clear"],
        ),
        engine: str | None = discord.Option(
            default=None,
            description="Engine to set as default (for action=set)",
        ),
    ) -> None:
        """Show or manage default engine selection."""
        if ctx.guild is None:
            await ctx.respond(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await require_allowed_user(ctx):
            return

        if runtime is None:
            await ctx.respond("Runtime not available.", ephemeral=True)
            return

        guild_id = ctx.guild.id
        channel_id = ctx.channel_id
        thread_id = None
        if isinstance(ctx.channel, discord.Thread):
            thread_id = ctx.channel_id
            channel_id = ctx.channel.parent_id or ctx.channel_id

        target_id = thread_id if thread_id is not None else channel_id
        scope = "thread" if thread_id is not None else "channel"

        normalized_action = (action or "show").strip().lower()
        if normalized_action in {"set", "clear"}:
            if not await _require_admin(ctx):
                return
            if normalized_action == "clear":
                await prefs_store.set_default_engine(guild_id, target_id, None)
                await ctx.respond(
                    f"Default agent override cleared for this {scope}.",
                    ephemeral=True,
                )
                return
            if engine is None:
                await ctx.respond(
                    "Missing engine. Example: `/agent action:set engine:codex`.",
                    ephemeral=True,
                )
                return
            if engine not in runtime.engine_ids:
                await ctx.respond(
                    f"Unknown engine `{engine}`. Available: {', '.join(runtime.engine_ids)}",
                    ephemeral=True,
                )
                return
            await prefs_store.set_default_engine(guild_id, target_id, engine)
            await ctx.respond(
                f"Default agent set to `{engine}` for this {scope}.",
                ephemeral=True,
            )
            return

        # Get available engines
        engines = list(runtime.engine_ids) if runtime.engine_ids else []
        if not engines:
            await ctx.respond("No engines configured.", ephemeral=True)
            return

        from .types import DiscordChannelContext, DiscordThreadContext

        bound_thread_default = None
        if thread_id is not None:
            ctx_data = await state_store.get_context(guild_id, thread_id)
            if isinstance(ctx_data, DiscordThreadContext):
                bound_thread_default = ctx_data.default_engine
        bound_channel_default = None
        ctx_data = await state_store.get_context(guild_id, channel_id)
        if isinstance(ctx_data, DiscordChannelContext):
            bound_channel_default = ctx_data.default_engine

        default_engine, source = await resolve_effective_default_engine(
            prefs_store,
            guild_id=guild_id,
            channel_id=channel_id,
            thread_id=thread_id,
            bound_thread_default=bound_thread_default,
            bound_channel_default=bound_channel_default,
            config_default=runtime.default_engine,
        )

        lines = ["**Available Agents**"]
        for engine in engines:
            marker = " (default)" if engine == default_engine else ""
            lines.append(f"- `{engine}`{marker}")

        if default_engine and source:
            pretty_source = {
                "thread_override": "thread override",
                "channel_override": "channel override",
                "thread_context": "thread context",
                "channel_context": "channel context",
                "config": "config default",
            }.get(source, source)
            lines.append(f"\n_Default: `{default_engine}` ({pretty_source})_")

        # Show any overrides
        overrides = await resolve_overrides(
            prefs_store, guild_id, channel_id, thread_id, default_engine or engines[0]
        )
        if overrides.model or overrides.reasoning:
            lines.append("\n**Overrides**")
            if overrides.model:
                lines.append(f"- Model: `{overrides.model}` ({overrides.source_model})")
            if overrides.reasoning:
                lines.append(
                    f"- Reasoning: `{overrides.reasoning}` ({overrides.source_reasoning})"
                )

        await ctx.respond("\n".join(lines), ephemeral=True)

    @pycord_bot.slash_command(
        name="model", description="Show or set model override for an engine"
    )
    async def model_command(
        ctx: discord.ApplicationContext,
        engine: str | None = discord.Option(
            default=None,
            description="Engine to configure (e.g., claude, codex)",
        ),
        model: str | None = discord.Option(
            default=None,
            description="Model to use (or 'clear' to remove override)",
        ),
    ) -> None:
        """Show or set model override."""
        if ctx.guild is None:
            await ctx.respond(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await require_allowed_user(ctx):
            return

        guild_id = ctx.guild.id
        channel_id = ctx.channel_id
        thread_id = None
        target_id = channel_id  # Where to store the override

        if isinstance(ctx.channel, discord.Thread):
            thread_id = ctx.channel_id
            target_id = thread_id  # Store on thread

        # Show current overrides
        if engine is None:
            model_overrides, _, _, _ = await prefs_store.get_all_overrides(
                guild_id, target_id
            )
            if not model_overrides:
                await ctx.respond("No model overrides set.", ephemeral=True)
                return
            lines = ["**Model Overrides**"]
            for eng, mod in model_overrides.items():
                lines.append(f"- `{eng}`: `{mod}`")
            await ctx.respond("\n".join(lines), ephemeral=True)
            return

        # Setting an override requires admin
        if model is not None:
            if not await _require_admin(ctx):
                return

            if model.lower() == "clear":
                await prefs_store.set_model_override(guild_id, target_id, engine, None)
                await ctx.respond(
                    f"Model override cleared for `{engine}`.", ephemeral=True
                )
            else:
                await prefs_store.set_model_override(guild_id, target_id, engine, model)
                await ctx.respond(
                    f"Model override set for `{engine}`: `{model}`", ephemeral=True
                )
            return

        # Show override for specific engine
        current = await prefs_store.get_model_override(guild_id, target_id, engine)
        if current:
            await ctx.respond(
                f"Model override for `{engine}`: `{current}`", ephemeral=True
            )
        else:
            await ctx.respond(f"No model override for `{engine}`.", ephemeral=True)

    @pycord_bot.slash_command(
        name="reasoning", description="Show or set reasoning level for an engine"
    )
    async def reasoning_command(
        ctx: discord.ApplicationContext,
        engine: str | None = discord.Option(
            default=None,
            description="Engine to configure (e.g., codex)",
        ),
        level: str | None = discord.Option(
            default=None,
            description="Reasoning level (minimal/low/medium/high/xhigh) or 'clear'",
        ),
    ) -> None:
        """Show or set reasoning level override."""
        if ctx.guild is None:
            await ctx.respond(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await require_allowed_user(ctx):
            return

        guild_id = ctx.guild.id
        channel_id = ctx.channel_id
        thread_id = None
        target_id = channel_id

        if isinstance(ctx.channel, discord.Thread):
            thread_id = ctx.channel_id
            target_id = thread_id

        # Show current overrides
        if engine is None:
            _, reasoning_overrides, _, _ = await prefs_store.get_all_overrides(
                guild_id, target_id
            )
            if not reasoning_overrides:
                await ctx.respond("No reasoning overrides set.", ephemeral=True)
                return
            lines = ["**Reasoning Overrides**"]
            for eng, lvl in reasoning_overrides.items():
                lines.append(f"- `{eng}`: `{lvl}`")
            await ctx.respond("\n".join(lines), ephemeral=True)
            return

        # Setting an override requires admin
        if level is not None:
            if not await _require_admin(ctx):
                return

            if level.lower() == "clear":
                await prefs_store.set_reasoning_override(
                    guild_id, target_id, engine, None
                )
                await ctx.respond(
                    f"Reasoning override cleared for `{engine}`.", ephemeral=True
                )
                return

            if not is_valid_reasoning_level(level.lower()):
                valid = ", ".join(sorted(REASONING_LEVELS))
                await ctx.respond(
                    f"Invalid reasoning level. Valid levels: {valid}", ephemeral=True
                )
                return

            if not supports_reasoning(engine):
                await ctx.respond(
                    f"Engine `{engine}` does not support reasoning overrides.",
                    ephemeral=True,
                )
                return

            await prefs_store.set_reasoning_override(
                guild_id, target_id, engine, level.lower()
            )
            await ctx.respond(
                f"Reasoning override set for `{engine}`: `{level.lower()}`",
                ephemeral=True,
            )
            return

        # Show override for specific engine
        current = await prefs_store.get_reasoning_override(guild_id, target_id, engine)
        if current:
            await ctx.respond(
                f"Reasoning override for `{engine}`: `{current}`", ephemeral=True
            )
        else:
            await ctx.respond(f"No reasoning override for `{engine}`.", ephemeral=True)

    @pycord_bot.slash_command(
        name="trigger", description="Show or set trigger mode (all/mentions)"
    )
    async def trigger_command(
        ctx: discord.ApplicationContext,
        mode: str | None = discord.Option(
            default=None,
            description="Trigger mode: all, mentions, or clear",
            choices=["all", "mentions", "clear"],
        ),
    ) -> None:
        """Show or set trigger mode."""
        if ctx.guild is None:
            await ctx.respond(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await require_allowed_user(ctx):
            return

        guild_id = ctx.guild.id
        channel_id = ctx.channel_id
        thread_id = None
        target_id = channel_id

        if isinstance(ctx.channel, discord.Thread):
            thread_id = ctx.channel_id
            target_id = thread_id
            channel_id = ctx.channel.parent_id or ctx.channel_id

        # Show current mode
        if mode is None:
            current = await resolve_trigger_mode(
                prefs_store,
                guild_id,
                channel_id,
                thread_id,
                default_mode=trigger_mode_default,
            )
            stored = await prefs_store.get_trigger_mode(guild_id, target_id)
            if stored:
                await ctx.respond(
                    f"Trigger mode: `{current}` (set on this {'thread' if thread_id else 'channel'})",
                    ephemeral=True,
                )
            else:
                await ctx.respond(
                    f"Trigger mode: `{current}` (inherited/default)", ephemeral=True
                )
            return

        # Setting requires admin
        if not await _require_admin(ctx):
            return

        if mode == "clear":
            await prefs_store.set_trigger_mode(guild_id, target_id, None)
            await ctx.respond("Trigger mode cleared (using default).", ephemeral=True)
        else:
            await prefs_store.set_trigger_mode(guild_id, target_id, mode)
            mode_desc = (
                "respond to all messages"
                if mode == "all"
                else "only respond when @mentioned or replied to"
            )
            await ctx.respond(
                f"Trigger mode set to `{mode}` ({mode_desc}).", ephemeral=True
            )

    # File transfer command (only if files is enabled)
    if files is not None and files.enabled and runtime is not None:
        from pathlib import Path

        from tunapi.config import ConfigError
        from tunapi.context import RunContext

        from .file_transfer import (
            MAX_FILE_SIZE,
            ZipTooLargeError,
            default_upload_name,
            deny_reason,
            format_bytes,
            normalize_relative_path,
            resolve_path_within_root,
            save_attachment_to_path,
            zip_directory,
        )
        from .types import DiscordChannelContext, DiscordThreadContext

        async def _get_project_root(
            ctx: discord.ApplicationContext,
        ) -> tuple[Path | None, RunContext | None]:
            """Get the project root directory for the current channel context."""
            if ctx.guild is None:
                return None, None

            guild_id = ctx.guild.id
            channel_id = ctx.channel_id
            if channel_id is None:
                return None, None

            # Get context - check thread first, then parent channel
            context = None
            channel = ctx.channel

            if isinstance(channel, discord.Thread):
                context = await state_store.get_context(guild_id, channel.id)
                if context is None and channel.parent_id:
                    context = await state_store.get_context(guild_id, channel.parent_id)
            else:
                context = await state_store.get_context(guild_id, channel_id)

            if context is None:
                return None, None

            # Build RunContext
            if isinstance(context, DiscordThreadContext):
                run_context = RunContext(
                    project=context.project,
                    branch=context.branch,
                )
            elif isinstance(context, DiscordChannelContext):
                run_context = RunContext(
                    project=context.project,
                    branch=context.worktree_base,
                )
            else:
                return None, None

            # Resolve working directory
            try:
                run_root = runtime.resolve_run_cwd(run_context)
            except ConfigError:
                return None, None

            return run_root, run_context

        @pycord_bot.slash_command(name="file", description="Upload or download files")
        async def file_command(
            ctx: discord.ApplicationContext,
            action: str = discord.Option(
                description="Action: get (download) or put (upload)",
                choices=["get", "put"],
            ),
            path: str = discord.Option(
                description="File path relative to project directory",
            ),
            force: bool = discord.Option(
                default=False,
                description="Overwrite existing files (put only)",
            ),
        ) -> None:
            """Handle file transfers."""
            if ctx.guild is None:
                await ctx.respond(
                    "This command can only be used in a server.", ephemeral=True
                )
                return
            if not await require_allowed_user(ctx, require_files=True):
                return

            # File operations require admin
            if not await _require_admin(ctx):
                return

            # Get project root from channel context
            project_root, run_context = await _get_project_root(ctx)
            if project_root is None:
                await ctx.respond(
                    "This channel is not bound to a project.\n"
                    "Use `/bind <project>` first to enable file transfers.",
                    ephemeral=True,
                )
                return

            deny_globs = files.deny_globs

            if action == "get":
                # Download file from server
                rel_path = normalize_relative_path(path)
                if rel_path is None:
                    await ctx.respond(
                        "Invalid path. Must be relative, no `..` or `.git`.",
                        ephemeral=True,
                    )
                    return

                denied = deny_reason(rel_path, deny_globs)
                if denied:
                    await ctx.respond(
                        f"Path denied by rule: `{denied}`", ephemeral=True
                    )
                    return

                target = resolve_path_within_root(project_root, rel_path)
                if target is None:
                    await ctx.respond("Path escapes project directory.", ephemeral=True)
                    return

                if not target.exists():
                    await ctx.respond(f"File not found: `{rel_path}`", ephemeral=True)
                    return

                await ctx.defer(ephemeral=True)

                try:
                    if target.is_dir():
                        # Zip the directory
                        try:
                            zip_data = zip_directory(
                                project_root,
                                rel_path,
                                deny_globs,
                                max_bytes=MAX_FILE_SIZE,
                            )
                        except ZipTooLargeError:
                            await ctx.followup.send(
                                f"Directory too large to zip (>{format_bytes(MAX_FILE_SIZE)}).",
                                ephemeral=True,
                            )
                            return
                        filename = f"{rel_path.name}.zip"
                        file = discord.File(
                            fp=__import__("io").BytesIO(zip_data),
                            filename=filename,
                        )
                        await ctx.followup.send(
                            f"Directory `{rel_path}` ({format_bytes(len(zip_data))})",
                            file=file,
                            ephemeral=True,
                        )
                    else:
                        # Send file directly
                        size = target.stat().st_size
                        if size > MAX_FILE_SIZE:
                            await ctx.followup.send(
                                f"File too large ({format_bytes(size)} > {format_bytes(MAX_FILE_SIZE)}).",
                                ephemeral=True,
                            )
                            return
                        file = discord.File(fp=str(target), filename=target.name)
                        await ctx.followup.send(
                            f"File `{rel_path}` ({format_bytes(size)})",
                            file=file,
                            ephemeral=True,
                        )
                except OSError as e:
                    await ctx.followup.send(f"Error reading file: {e}", ephemeral=True)

            elif action == "put":
                raw_path = path.strip()
                path_is_dir_hint = raw_path.endswith(("/", "\\"))
                rel_path = normalize_relative_path(raw_path)
                if rel_path is None:
                    await ctx.respond(
                        "Invalid path. Must be relative, no `..` or `.git`.",
                        ephemeral=True,
                    )
                    return

                # Gather attachments from the command message, or from the replied-to message.
                attachments: list[discord.Attachment] = []
                ref_id: int | None = None
                if ctx.message is not None:
                    attachments = list(ctx.message.attachments or [])
                    if ctx.message.reference and ctx.message.reference.message_id:
                        ref_id = ctx.message.reference.message_id

                if not attachments and ref_id is not None:
                    try:
                        ref_msg = await ctx.channel.fetch_message(ref_id)
                    except (discord.NotFound, discord.HTTPException):
                        ref_msg = None
                    if ref_msg is not None:
                        attachments = list(ref_msg.attachments or [])

                if not attachments:
                    await ctx.respond(
                        "Attach file(s) to this command, or reply to a message with attachments and run `/file put`.",
                        ephemeral=True,
                    )
                    return

                # Decide whether `path` is a directory target.
                dir_mode = len(attachments) > 1
                base_target = resolve_path_within_root(project_root, rel_path)
                if base_target is None:
                    await ctx.respond("Path escapes project directory.", ephemeral=True)
                    return
                if dir_mode:
                    if base_target.exists() and base_target.is_file():
                        await ctx.respond(
                            "For multiple files, `path` must be a directory.",
                            ephemeral=True,
                        )
                        return
                else:
                    dir_mode = path_is_dir_hint or (
                        base_target.exists() and base_target.is_dir()
                    )

                await ctx.defer(ephemeral=True)

                results: list[str] = []
                for attachment in attachments:
                    dest_rel = rel_path
                    if dir_mode:
                        dest_rel = rel_path / default_upload_name(attachment.filename)

                    result = await save_attachment_to_path(
                        attachment,
                        project_root,
                        dest_rel,
                        deny_globs,
                        max_bytes=files.max_upload_bytes,
                        force=force,
                    )
                    if result.error is not None:
                        results.append(f"❌ `{attachment.filename}`: {result.error}")
                        continue
                    if result.rel_path is None or result.size is None:
                        results.append(f"❌ `{attachment.filename}`: failed to save")
                        continue
                    overwritten = " (overwritten)" if result.overwritten else ""
                    results.append(
                        f"✅ `{result.rel_path.as_posix()}` ({format_bytes(result.size)}){overwritten}"
                    )

                await ctx.followup.send("\n".join(results), ephemeral=True)

    # Voice commands (only register if voice_manager is provided)
    if voice_manager is not None:
        _register_voice_commands(
            bot,
            state_store=state_store,
            voice_manager=voice_manager,
            allowed_user_ids=allowed_user_ids,
        )


def _register_voice_commands(
    bot: DiscordBotClient,
    *,
    state_store: DiscordStateStore,
    voice_manager: VoiceManager,
    allowed_user_ids: frozenset[int] | None,
) -> None:
    """Register voice-related slash commands."""
    from .types import DiscordThreadContext

    pycord_bot = bot.bot

    @pycord_bot.slash_command(
        name="voice",
        description="Create a voice channel for this thread/channel and join it",
    )
    async def voice_command(ctx: discord.ApplicationContext) -> None:
        """Create a voice channel bound to the current thread/channel's project context."""
        if ctx.guild is None:
            await ctx.respond(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        user_id = getattr(getattr(ctx, "author", None), "id", None)
        if not isinstance(user_id, int):
            user_id = None
        if not is_user_allowed(allowed_user_ids, user_id):
            await ctx.respond("You are not allowed to use this bot.", ephemeral=True)
            return

        guild_id = ctx.guild.id
        channel = ctx.channel

        # Determine the text channel ID and get context
        text_channel_id = ctx.channel_id
        if text_channel_id is None:
            await ctx.respond("Could not determine the channel.", ephemeral=True)
            return

        # Get context - check thread first, then parent channel
        context = None

        if isinstance(channel, discord.Thread):
            # Try thread-specific context first
            context = await state_store.get_context(guild_id, channel.id)
            if context is None and channel.parent_id:
                # Fall back to parent channel context
                context = await state_store.get_context(guild_id, channel.parent_id)
        else:
            context = await state_store.get_context(guild_id, text_channel_id)

        if context is None:
            await ctx.respond(
                "This channel/thread is not bound to a project.\n"
                "Use `/bind <project>` first, then `/voice`.",
                ephemeral=True,
            )
            return

        # Defer since creating channel and joining might take a moment
        await ctx.defer(ephemeral=True)

        # Determine the branch name for the voice channel
        if isinstance(context, DiscordThreadContext):
            branch = context.branch
        else:
            branch = context.worktree_base

        try:
            # Create a temporary voice channel
            if isinstance(channel, discord.Thread):
                voice_name = f"Voice: {channel.name[:90]}"
            else:
                voice_name = f"Voice: {branch}"

            # Get the category of the current channel (if any)
            category = None
            if isinstance(channel, discord.Thread) and channel.parent:
                category = channel.parent.category
            elif isinstance(channel, discord.TextChannel):
                category = channel.category

            voice_channel = await ctx.guild.create_voice_channel(
                name=voice_name,
                category=category,
                reason=f"Voice session for {context.project}:{branch}",
            )

            # Join the voice channel
            await voice_manager.join_channel(
                voice_channel,
                text_channel_id,
                context.project,
                branch,
            )

            await ctx.followup.send(
                f"Created voice channel **{voice_channel.name}**.\n"
                f"Project: `{context.project}` Branch: `{branch}`\n"
                f"Join to start talking. The channel will be deleted when everyone leaves.",
            )
        except discord.Forbidden:
            await ctx.followup.send(
                "I don't have permission to create voice channels.",
                ephemeral=True,
            )
        except discord.ClientException as e:
            await ctx.followup.send(
                f"Failed to create/join voice channel: {e}",
                ephemeral=True,
            )

    # Register /vc as an alias for /voice
    @pycord_bot.slash_command(
        name="vc",
        description="Create a voice channel for this thread/channel (alias for /voice)",
    )
    async def vc_command(ctx: discord.ApplicationContext) -> None:
        """Alias for /voice command."""
        await voice_command(ctx)


def register_engine_commands(
    bot: DiscordBotClient,
    *,
    cfg: DiscordBridgeConfig,
    state_store: DiscordStateStore,
    prefs_store: DiscordPrefsStore,
    running_tasks: RunningTasks,
    default_engine_override: str | None = None,
) -> list[str]:
    """Register dynamic slash commands for each available engine.

    Creates commands like /claude, /codex, /pi that directly invoke
    the corresponding engine with the prompt text.

    Args:
        bot: The Discord bot client
        cfg: Bridge configuration
        state_store: State store for resolving context
        prefs_store: Preferences store for overrides
        running_tasks: Running tasks dictionary
        default_engine_override: Default engine override

    Returns:
        List of registered engine command names
    """
    from tunapi.logging import get_logger

    logger = get_logger(__name__)
    pycord_bot = bot.bot
    runtime = cfg.runtime

    registered: list[str] = []

    for engine_id in runtime.available_engine_ids():
        cmd_name = engine_id.lower()
        description = f"Use agent: {cmd_name}"

        # Create a factory function to capture engine_id in closure
        def make_engine_command(eng_id: str, cmd: str, desc: str):
            @pycord_bot.slash_command(name=cmd, description=desc)
            async def engine_command(
                ctx: discord.ApplicationContext,
                prompt: str = discord.Option(
                    description="The prompt to send to the agent"
                ),
            ) -> None:
                await _handle_engine_command(
                    ctx,
                    engine_id=eng_id,
                    prompt=prompt,
                    cfg=cfg,
                    state_store=state_store,
                    prefs_store=prefs_store,
                    running_tasks=running_tasks,
                )

            return engine_command

        make_engine_command(engine_id, cmd_name, description)
        registered.append(cmd_name)
        logger.info("engine_command.registered", engine=engine_id, command=cmd_name)

    return registered


def _format_engine_starter_message(
    engine_id: str,
    prompt: str,
    *,
    max_chars: int = 2000,
) -> str:
    prefix = f"/{engine_id.lower()} "
    if len(prefix) + len(prompt) <= max_chars:
        return prefix + prompt
    slice_len = max(0, max_chars - len(prefix) - 1)
    return prefix + prompt[:slice_len] + "…"


async def _handle_engine_command(
    ctx: discord.ApplicationContext,
    *,
    engine_id: str,
    prompt: str,
    cfg: DiscordBridgeConfig,
    state_store: DiscordStateStore,
    prefs_store: DiscordPrefsStore,
    running_tasks: RunningTasks,
) -> None:
    """Handle a dynamic engine slash command invocation."""
    import anyio

    from tunapi.context import RunContext
    from tunapi.logging import get_logger
    from tunapi.model import ResumeToken
    from tunapi.runners.run_options import EngineRunOptions
    from tunapi.transport import MessageRef

    from .commands.executor import _run_engine
    from .types import DiscordChannelContext, DiscordThreadContext

    logger = get_logger(__name__)

    if ctx.guild is None:
        await ctx.respond("This command can only be used in a server.", ephemeral=True)
        return
    user_id = getattr(getattr(ctx, "author", None), "id", None)
    if not isinstance(user_id, int):
        user_id = None
    if not is_user_allowed(cfg.allowed_user_ids, user_id):
        await ctx.respond("You are not allowed to use this bot.", ephemeral=True)
        return

    # Defer quickly, then run in background so the interaction doesn't time out.
    await ctx.defer(ephemeral=True)

    guild_id = ctx.guild.id
    channel_id = ctx.channel_id
    thread_id: int | None = None
    created_new_thread = False
    attempted_thread_create = False

    if isinstance(ctx.channel, discord.Thread):
        thread_id = ctx.channel_id
        if ctx.channel.parent_id:
            channel_id = ctx.channel.parent_id

    # Create a new thread when invoked in a channel (mirrors message behavior).
    if thread_id is None and isinstance(ctx.channel, discord.TextChannel):
        attempted_thread_create = True
        thread_name = prompt[:100] if len(prompt) <= 100 else prompt[:97] + "..."
        created_thread_id = await cfg.bot.create_thread_without_message(
            channel_id=channel_id,
            name=thread_name,
        )
        if created_thread_id is not None:
            thread_id = created_thread_id
            created_new_thread = True
            logger.info(
                "engine_command.thread_created",
                engine=engine_id,
                channel_id=channel_id,
                thread_id=thread_id,
                name=thread_name,
            )

    # Resolve context from state (same logic as message handling)
    run_context: RunContext | None = None
    channel_context: DiscordChannelContext | None = None
    thread_context: DiscordThreadContext | None = None

    if thread_id is not None:
        ctx_data = await state_store.get_context(guild_id, thread_id)
        if isinstance(ctx_data, DiscordThreadContext):
            thread_context = ctx_data

    ctx_data = await state_store.get_context(guild_id, channel_id)
    if isinstance(ctx_data, DiscordChannelContext):
        channel_context = ctx_data

    if thread_context:
        run_context = RunContext(
            project=thread_context.project,
            branch=thread_context.branch,
        )
    elif channel_context:
        run_context = RunContext(
            project=channel_context.project,
            branch=channel_context.worktree_base,
        )

    # If we created a new thread in a bound channel, persist the thread context so
    # follow-up messages default to the requested engine.
    if created_new_thread and channel_context is not None and thread_id is not None:
        new_thread_context = DiscordThreadContext(
            project=channel_context.project,
            branch=channel_context.worktree_base,
            worktrees_dir=channel_context.worktrees_dir,
            default_engine=engine_id,
        )
        await state_store.set_context(guild_id, thread_id, new_thread_context)
        logger.info(
            "engine_command.thread_context_saved",
            thread_id=thread_id,
            project=channel_context.project,
            branch=channel_context.worktree_base,
            engine=engine_id,
        )

    # Resolve model and reasoning overrides for this engine
    overrides = await resolve_overrides(
        prefs_store, guild_id, channel_id, thread_id, engine_id
    )
    run_options: EngineRunOptions | None = None
    if overrides.model or overrides.reasoning:
        run_options = EngineRunOptions(
            model=overrides.model,
            reasoning=overrides.reasoning,
        )
        logger.debug(
            "engine_command.overrides",
            engine=engine_id,
            model=overrides.model,
            reasoning=overrides.reasoning,
        )

    # Use the effective channel (thread if in one, otherwise channel)
    effective_channel_id = thread_id or channel_id

    # Seed the conversation with a real message so progress can reply and cancel works reliably.
    starter_msg = await cfg.bot.send_message(
        channel_id=effective_channel_id,
        content=_format_engine_starter_message(engine_id, prompt),
    )
    if starter_msg is None:
        await ctx.followup.send(
            f"Failed to post the prompt in <#{effective_channel_id}>.",
            ephemeral=True,
        )
        return

    # Restore session if one exists for this engine in the channel/thread.
    resume_token: ResumeToken | None = None
    author_id = getattr(getattr(ctx, "author", None), "id", None)
    if not isinstance(author_id, int):
        author_id = None
    session_key = thread_id if thread_id is not None else channel_id
    if cfg.session_mode == "chat":
        token_str = await state_store.get_session(
            guild_id,
            session_key,
            engine_id,
            author_id=author_id,
        )
        if token_str:
            resume_token = ResumeToken(engine=engine_id, value=token_str)

    logger.info(
        "engine_command.run",
        engine=engine_id,
        guild_id=guild_id,
        channel_id=effective_channel_id,
        prompt_length=len(prompt),
        has_context=run_context is not None,
    )

    async def on_thread_known(new_token: ResumeToken, _event: anyio.Event) -> None:
        await state_store.set_session(
            guild_id,
            session_key,
            engine_id,
            new_token.value,
            author_id=author_id,
        )
        logger.info(
            "engine_command.session_saved",
            guild_id=guild_id,
            session_key=session_key,
            author_id=author_id,
            engine=engine_id,
        )

    async def run_engine_job() -> None:
        await _run_engine(
            exec_cfg=cfg.exec_cfg,
            runtime=cfg.runtime,
            running_tasks=running_tasks,
            channel_id=effective_channel_id,
            user_msg_id=starter_msg.message_id,
            text=prompt,
            resume_token=resume_token,
            context=run_context,
            reply_ref=MessageRef(
                channel_id=effective_channel_id,
                message_id=starter_msg.message_id,
                thread_id=thread_id,
            ),
            on_thread_known=on_thread_known,
            engine_override=engine_id,
            thread_id=thread_id,
            show_resume_line=cfg.show_resume_line,
            run_options=run_options,
        )

    asyncio.create_task(
        run_engine_job(),
        name=f"tunapi-discord:engine:{engine_id}:{effective_channel_id}",
    )

    target = f"<#{thread_id}>" if thread_id is not None else f"<#{channel_id}>"
    note = (
        " (thread creation failed; running in channel)"
        if attempted_thread_create and thread_id is None
        else ""
    )
    await ctx.followup.send(
        f"Started `/{engine_id.lower()}` in {target}{note}.",
        ephemeral=True,
    )


def is_bot_mentioned(message: discord.Message, bot_user: discord.User | None) -> bool:
    """Check if the bot is mentioned in the message."""
    if bot_user is None:
        return False
    return bot_user in message.mentions


def should_process_message(
    message: discord.Message,
    bot_user: discord.User | None,
    *,
    require_mention: bool = False,
) -> bool:
    """Determine if a message should be processed by the bot.

    Args:
        message: The Discord message
        bot_user: The bot's user object
        require_mention: If True, only process messages that mention the bot
    """
    # Ignore bot messages
    if message.author.bot:
        return False

    # Ignore empty messages (but allow if there are attachments for auto_put)
    if not message.content.strip() and not message.attachments:
        return False

    # In threads, always process
    if isinstance(message.channel, discord.Thread):
        return True

    # In channels, check if mention is required
    if require_mention:
        return is_bot_mentioned(message, bot_user)

    return True


def extract_prompt_from_message(
    message: discord.Message,
    bot_user: discord.User | None,
) -> str:
    """Extract the prompt text from a message, removing bot mentions."""
    content = message.content

    # Remove bot mention if present
    if bot_user is not None:
        content = content.replace(f"<@{bot_user.id}>", "").strip()
        content = content.replace(f"<@!{bot_user.id}>", "").strip()

    return content


def parse_branch_prefix(content: str) -> tuple[str | None, str]:
    """Parse @branch prefix from message content.

    Returns (branch, remaining_prompt).

    Examples:
        "@chore/hello fix the bug" -> ("chore/hello", "fix the bug")
        "@feat-login" -> ("feat-login", "")
        "hello world" -> (None, "hello world")
    """
    content = content.strip()
    if not content.startswith("@"):
        return None, content

    # Find the end of the branch token (first whitespace or end of string)
    parts = content[1:].split(None, 1)  # Split on whitespace, max 2 parts
    if not parts:
        return None, content

    branch = parts[0]
    if not branch:
        return None, content

    remaining = parts[1] if len(parts) > 1 else ""
    return branch, remaining.strip()
