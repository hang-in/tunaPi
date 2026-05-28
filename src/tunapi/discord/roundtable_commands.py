from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from tunapi.runner_bridge import RunningTasks

    from .bridge import DiscordBridgeConfig
    from .client import DiscordBotClient
    from .state import DiscordStateStore


def register_roundtable_command(
    bot: DiscordBotClient,
    *,
    cfg: DiscordBridgeConfig,
    running_tasks: RunningTasks,
    roundtable_store: object,
    state_store: DiscordStateStore,
    allowed_user_ids: frozenset[int] | None = None,
    is_user_allowed_fn: Any,
    discord_module=discord,
) -> None:
    """Register the /rt slash command for roundtable discussions."""
    from tunapi.context import RunContext
    from tunapi.core.roundtable import RoundtableStore

    from .types import DiscordChannelContext, DiscordThreadContext

    pycord_bot = bot.bot
    runtime = cfg.runtime
    roundtables: RoundtableStore = roundtable_store  # type: ignore[assignment]

    @pycord_bot.slash_command(
        name="rt",
        description="Start a multi-agent roundtable discussion",
    )
    async def rt_command(
        ctx: discord.ApplicationContext,
        topic: str = discord.Option(
            description='Topic for discussion (e.g., "best approach to caching")',
        ),
        rounds: int = discord.Option(
            default=1,
            description="Number of discussion rounds (default: 1)",
        ),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        user_id = getattr(getattr(ctx, "author", None), "id", None)
        if not isinstance(user_id, int):
            user_id = None
        if not is_user_allowed_fn(allowed_user_ids, user_id):
            await ctx.respond("You are not allowed to use this bot.", ephemeral=True)
            return

        rt_config = runtime.roundtable
        rt_engines = list(rt_config.engines) or list(runtime.available_engine_ids())

        if not rt_engines:
            await ctx.respond("No engines available for roundtable.", ephemeral=True)
            return

        if rounds < 1:
            await ctx.respond("Rounds must be at least 1.", ephemeral=True)
            return
        if rounds > rt_config.max_rounds:
            await ctx.respond(
                f"Maximum {rt_config.max_rounds} rounds allowed.", ephemeral=True
            )
            return

        guild_id = ctx.guild.id
        channel_id = ctx.channel_id
        thread_id = None

        if isinstance(ctx.channel, discord_module.Thread):
            thread_id = ctx.channel_id
            channel_id = ctx.channel.parent_id or ctx.channel_id

        run_context: RunContext | None = None
        if thread_id:
            ctx_data = await state_store.get_context(guild_id, thread_id)
            if isinstance(ctx_data, DiscordThreadContext):
                run_context = RunContext(
                    project=ctx_data.project,
                    branch=ctx_data.branch,
                )
        if run_context is None:
            ctx_data = await state_store.get_context(guild_id, channel_id)
            if isinstance(ctx_data, DiscordChannelContext):
                run_context = RunContext(
                    project=ctx_data.project,
                    branch=ctx_data.worktree_base,
                )

        target_channel = thread_id if thread_id else channel_id
        engines_display = ", ".join(f"`{e}`" for e in rt_engines)
        await ctx.respond(
            f"Starting roundtable: **{topic}**\n"
            f"Engines: {engines_display} | Rounds: {rounds}",
            ephemeral=True,
        )

        from .loop import _start_roundtable

        asyncio.create_task(
            _start_roundtable(
                target_channel,
                topic,
                rounds,
                rt_engines,
                cfg=cfg,
                running_tasks=running_tasks,
                roundtables=roundtables,
                run_context=run_context,
            ),
            name=f"tunapi-discord:roundtable:{target_channel}",
        )
