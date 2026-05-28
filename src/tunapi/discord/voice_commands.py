from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from .client import DiscordBotClient
    from .state import DiscordStateStore
    from .voice import VoiceManager


def register_voice_commands(
    bot: DiscordBotClient,
    *,
    state_store: DiscordStateStore,
    voice_manager: VoiceManager,
    allowed_user_ids: frozenset[int] | None,
    is_user_allowed_fn: Any,
    discord_module=discord,
) -> None:
    """Register voice-related slash commands."""
    from .types import DiscordThreadContext

    pycord_bot = bot.bot

    @pycord_bot.slash_command(
        name="voice",
        description="Create a voice channel for this thread/channel and join it",
    )
    async def voice_command(ctx: discord.ApplicationContext) -> None:
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

        guild_id = ctx.guild.id
        channel = ctx.channel

        text_channel_id = ctx.channel_id
        if text_channel_id is None:
            await ctx.respond("Could not determine the channel.", ephemeral=True)
            return

        context = None

        if isinstance(channel, discord_module.Thread):
            context = await state_store.get_context(guild_id, channel.id)
            if context is None and channel.parent_id:
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

        await ctx.defer(ephemeral=True)

        if isinstance(context, DiscordThreadContext):
            branch = context.branch
        else:
            branch = context.worktree_base

        try:
            if isinstance(channel, discord_module.Thread):
                voice_name = f"Voice: {channel.name[:90]}"
            else:
                voice_name = f"Voice: {branch}"

            category = None
            if isinstance(channel, discord_module.Thread) and channel.parent:
                category = channel.parent.category
            elif isinstance(channel, discord_module.TextChannel):
                category = channel.category

            voice_channel = await ctx.guild.create_voice_channel(
                name=voice_name,
                category=category,
                reason=f"Voice session for {context.project}:{branch}",
            )

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
        except discord_module.Forbidden:
            await ctx.followup.send(
                "I don't have permission to create voice channels.",
                ephemeral=True,
            )
        except discord_module.ClientException as e:
            await ctx.followup.send(
                f"Failed to create/join voice channel: {e}",
                ephemeral=True,
            )

    @pycord_bot.slash_command(
        name="vc",
        description="Create a voice channel for this thread/channel (alias for /voice)",
    )
    async def vc_command(ctx: discord.ApplicationContext) -> None:
        await voice_command(ctx)
