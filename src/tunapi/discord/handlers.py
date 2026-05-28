"""Slash command and message handlers for Discord."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

import discord

from .allowlist import is_user_allowed
from .ctx_commands import (
    _handle_ctx_command as _handle_ctx_command_impl,
    _is_admin as _is_admin,
    _normalize_branch_name as _normalize_branch_name,
    _require_admin as _require_admin,
)
from .overrides import (
    REASONING_LEVELS,
    is_valid_reasoning_level,
    resolve_effective_default_engine,
    resolve_overrides,
    resolve_trigger_mode,
    supports_reasoning,
)
from .message_utils import (
    extract_prompt_from_message as extract_prompt_from_message,
    is_bot_mentioned as is_bot_mentioned,
    parse_branch_prefix as parse_branch_prefix,
    should_process_message as should_process_message,
)
from .engine_commands import (
    _format_engine_starter_message as _format_engine_starter_message,
    handle_engine_command as _handle_engine_command_impl,
    register_engine_commands as _register_engine_commands_impl,
)
from .file_commands import register_file_command
from .roundtable_commands import (
    register_roundtable_command as _register_roundtable_command_impl,
)
from .voice_commands import register_voice_commands as _register_voice_commands_impl

if TYPE_CHECKING:
    from tunapi.runner_bridge import RunningTasks
    from tunapi.transport_runtime import TransportRuntime

    from .bridge import DiscordBridgeConfig, DiscordFilesSettings
    from .client import DiscordBotClient
    from .prefs import DiscordPrefsStore
    from .state import DiscordStateStore
    from .voice import VoiceManager


async def _handle_ctx_command(
    ctx: discord.ApplicationContext,
    *,
    action: str | None,
    project: str | None,
    branch: str | None,
    state_store: DiscordStateStore,
) -> None:
    await _handle_ctx_command_impl(
        ctx,
        action=action,
        project=project,
        branch=branch,
        state_store=state_store,
        require_admin=_require_admin,
    )


def register_slash_commands(
    bot: DiscordBotClient,
    *,
    state_store: DiscordStateStore,
    prefs_store: DiscordPrefsStore,
    get_running_task: Callable[..., object],
    cancel_task: Callable[..., object],
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
        register_file_command(
            pycord_bot,
            files=files,
            runtime=runtime,
            state_store=state_store,
            require_allowed_user=require_allowed_user,
            require_admin=_require_admin,
            discord_module=discord,
        )

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
    _register_voice_commands_impl(
        bot,
        state_store=state_store,
        voice_manager=voice_manager,
        allowed_user_ids=allowed_user_ids,
        is_user_allowed_fn=is_user_allowed,
        discord_module=discord,
    )


def register_engine_commands(
    bot: DiscordBotClient,
    *,
    cfg: DiscordBridgeConfig,
    state_store: DiscordStateStore,
    prefs_store: DiscordPrefsStore,
    running_tasks: RunningTasks,
    default_engine_override: str | None = None,
) -> list[str]:
    _ = default_engine_override
    return _register_engine_commands_impl(
        bot,
        cfg=cfg,
        state_store=state_store,
        prefs_store=prefs_store,
        running_tasks=running_tasks,
        handle_engine_command=_handle_engine_command,
    )


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
    await _handle_engine_command_impl(
        ctx,
        engine_id=engine_id,
        prompt=prompt,
        cfg=cfg,
        state_store=state_store,
        prefs_store=prefs_store,
        running_tasks=running_tasks,
        is_user_allowed_fn=is_user_allowed,
        resolve_overrides_fn=resolve_overrides,
        discord_module=discord,
    )


def register_roundtable_command(
    bot: DiscordBotClient,
    *,
    cfg: DiscordBridgeConfig,
    running_tasks: RunningTasks,
    roundtable_store: object,
    state_store: DiscordStateStore,
    allowed_user_ids: frozenset[int] | None = None,
) -> None:
    _register_roundtable_command_impl(
        bot,
        cfg=cfg,
        running_tasks=running_tasks,
        roundtable_store=roundtable_store,
        state_store=state_store,
        allowed_user_ids=allowed_user_ids,
        is_user_allowed_fn=is_user_allowed,
        discord_module=discord,
    )
