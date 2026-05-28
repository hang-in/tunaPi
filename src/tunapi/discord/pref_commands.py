"""Per-channel preference slash commands for Discord.

Extracted from ``handlers.register_slash_commands``. Covers the override
commands: ``/agent`` (default engine), ``/model``, ``/reasoning`` and
``/trigger``. Behaviour is identical to the inline closures it replaced.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal, cast

import discord

from .overrides import (
    REASONING_LEVELS,
    is_valid_reasoning_level,
    resolve_effective_default_engine,
    resolve_overrides,
    resolve_trigger_mode,
    supports_reasoning,
)

if TYPE_CHECKING:
    from tunapi.transport_runtime import TransportRuntime

    from .client import DiscordBotClient
    from .prefs import DiscordPrefsStore
    from .state import DiscordStateStore


def register_pref_commands(
    bot: DiscordBotClient,
    *,
    state_store: DiscordStateStore,
    prefs_store: DiscordPrefsStore,
    runtime: TransportRuntime | None,
    require_allowed_user: Callable[..., Awaitable[bool]],
    require_admin: Callable[..., Awaitable[bool]],
    trigger_mode_default: Literal["all", "mentions"] = "all",
) -> None:
    """Register agent/model/reasoning/trigger override slash commands."""
    pycord_bot = bot.bot

    @pycord_bot.slash_command(name="agent", description="Show or manage default agent")
    async def agent_command(
        ctx: discord.ApplicationContext,
        action: str | None = cast(
            str | None,
            discord.Option(
                default=None,
                description="Action to perform (show, set, clear)",
                choices=["show", "set", "clear"],
            ),
        ),
        engine: str | None = cast(
            str | None,
            discord.Option(
                default=None,
                description="Engine to set as default (for action=set)",
            ),
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
        if (
            channel_id is None
        ):  # pragma: no cover - Pycord guild commands have a channel
            await ctx.respond("This command requires a channel.", ephemeral=True)
            return
        thread_id: int | None = None
        if isinstance(ctx.channel, discord.Thread):
            thread_id = channel_id
            channel_id = ctx.channel.parent_id or channel_id

        target_id = thread_id if thread_id is not None else channel_id
        scope = "thread" if thread_id is not None else "channel"

        normalized_action = (action or "show").strip().lower()
        if normalized_action in {"set", "clear"}:
            if not await require_admin(ctx):
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
        engine: str | None = cast(
            str | None,
            discord.Option(
                default=None,
                description="Engine to configure (e.g., claude, codex)",
            ),
        ),
        model: str | None = cast(
            str | None,
            discord.Option(
                default=None,
                description="Model to use (or 'clear' to remove override)",
            ),
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
        if (
            channel_id is None
        ):  # pragma: no cover - Pycord guild commands have a channel
            await ctx.respond("This command requires a channel.", ephemeral=True)
            return
        thread_id: int | None = None
        target_id = channel_id  # Where to store the override

        if isinstance(ctx.channel, discord.Thread):
            thread_id = channel_id
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
            if not await require_admin(ctx):
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
        engine: str | None = cast(
            str | None,
            discord.Option(
                default=None,
                description="Engine to configure (e.g., codex)",
            ),
        ),
        level: str | None = cast(
            str | None,
            discord.Option(
                default=None,
                description="Reasoning level (minimal/low/medium/high/xhigh) or 'clear'",
            ),
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
        if (
            channel_id is None
        ):  # pragma: no cover - Pycord guild commands have a channel
            await ctx.respond("This command requires a channel.", ephemeral=True)
            return
        thread_id: int | None = None
        target_id = channel_id

        if isinstance(ctx.channel, discord.Thread):
            thread_id = channel_id
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
            if not await require_admin(ctx):
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
        mode: str | None = cast(
            str | None,
            discord.Option(
                default=None,
                description="Trigger mode: all, mentions, or clear",
                choices=["all", "mentions", "clear"],
            ),
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
        if (
            channel_id is None
        ):  # pragma: no cover - Pycord guild commands have a channel
            await ctx.respond("This command requires a channel.", ephemeral=True)
            return
        thread_id: int | None = None
        target_id = channel_id

        if isinstance(ctx.channel, discord.Thread):
            thread_id = channel_id
            target_id = thread_id
            channel_id = ctx.channel.parent_id or channel_id

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
        if not await require_admin(ctx):
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
