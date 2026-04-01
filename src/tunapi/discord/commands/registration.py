"""Dynamic slash command registration for plugin commands."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord

from tunapi.commands import get_command, list_command_ids
from tunapi.logging import get_logger
from tunapi.model import EngineId, ResumeToken
from tunapi.runner_bridge import RunningTasks
from tunapi.runners.run_options import EngineRunOptions

from .dispatch import dispatch_command
from ..allowlist import is_user_allowed

if TYPE_CHECKING:
    from ..bridge import DiscordBridgeConfig
    from ..client import DiscordBotClient
    from ..prefs import DiscordPrefsStore
    from ..state import DiscordStateStore

logger = get_logger(__name__)


def discover_command_ids(allowlist: set[str] | None) -> set[str]:
    """Discover available command plugin IDs."""
    return {cmd_id.lower() for cmd_id in list_command_ids(allowlist=allowlist)}


def _format_plugin_starter_message(
    command_id: str,
    args_text: str,
    *,
    max_chars: int = 2000,
) -> str:
    """Format a starter message for a plugin slash command."""
    full = f"/{command_id} {args_text}".strip()
    if len(full) <= max_chars:
        return full
    slice_len = max(0, max_chars - 1)
    return full[:slice_len] + "…"


def register_plugin_commands(
    bot: DiscordBotClient,
    cfg: DiscordBridgeConfig,
    *,
    command_ids: set[str],
    running_tasks: RunningTasks,
    state_store: DiscordStateStore,
    prefs_store: DiscordPrefsStore,
    default_engine_override: EngineId | None,
) -> None:
    """Register slash commands for discovered plugins.

    Args:
        bot: The Discord bot client
        cfg: Bridge configuration
        command_ids: Set of plugin command IDs to register
        running_tasks: Running tasks dictionary for cancellation
        state_store: State store (context/sessions)
        prefs_store: Preferences store for resolving overrides
        default_engine_override: Default engine override
    """
    pycord_bot = bot.bot

    for command_id in sorted(command_ids):
        backend = get_command(
            command_id, allowlist=cfg.runtime.allowlist, required=False
        )
        if backend is None:
            logger.warning("plugin.not_found", command_id=command_id)
            continue

        # Truncate description to Discord's 100 char limit
        description = backend.description
        if len(description) > 100:
            description = description[:97] + "..."

        # Create a factory function to capture command_id in closure
        def make_command(cmd_id: str, desc: str):
            @pycord_bot.slash_command(name=cmd_id, description=desc)
            async def plugin_command(
                ctx: discord.ApplicationContext,
                args: str = discord.Option(default="", description="Command arguments"),
            ) -> None:
                await _handle_plugin_command(
                    ctx,
                    command_id=cmd_id,
                    args_text=args,
                    cfg=cfg,
                    running_tasks=running_tasks,
                    state_store=state_store,
                    prefs_store=prefs_store,
                    default_engine_override=default_engine_override,
                )

            return plugin_command

        make_command(command_id, description)
        logger.info("plugin.registered", command_id=command_id, description=description)


async def _handle_plugin_command(
    ctx: discord.ApplicationContext,
    *,
    command_id: str,
    args_text: str,
    cfg: DiscordBridgeConfig,
    running_tasks: RunningTasks,
    state_store: DiscordStateStore,
    prefs_store: DiscordPrefsStore,
    default_engine_override: EngineId | None,
) -> None:
    """Handle a plugin slash command invocation."""
    import anyio

    from ..overrides import resolve_overrides

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
    author_id = getattr(getattr(ctx, "author", None), "id", None)
    if not isinstance(author_id, int):
        author_id = None
    channel_id = ctx.channel_id
    thread_id = None

    if isinstance(ctx.channel, discord.Thread):
        thread_id = ctx.channel_id
        if ctx.channel.parent_id:
            channel_id = ctx.channel.parent_id

    # Create engine overrides resolver
    async def engine_overrides_resolver(
        engine_id: EngineId,
    ) -> EngineRunOptions | None:
        overrides = await resolve_overrides(
            prefs_store, guild_id, channel_id, thread_id, engine_id
        )
        if overrides.model or overrides.reasoning:
            return EngineRunOptions(
                model=overrides.model,
                reasoning=overrides.reasoning,
            )
        return None

    # Build full text as it would appear in a message
    full_text = f"/{command_id} {args_text}".strip()

    # Seed with a real message so command replies/progress can reply reliably.
    effective_channel_id = thread_id or channel_id
    starter_msg = await cfg.bot.send_message(
        channel_id=effective_channel_id,
        content=_format_plugin_starter_message(command_id, args_text),
    )
    if starter_msg is None:
        await ctx.followup.send(
            f"Failed to post in <#{effective_channel_id}>.",
            ephemeral=True,
        )
        return

    message_id = starter_msg.message_id
    session_key = thread_id if thread_id is not None else channel_id

    async def on_thread_known(new_token: ResumeToken, _event: anyio.Event) -> None:
        await state_store.set_session(
            guild_id,
            session_key,
            new_token.engine,
            new_token.value,
            author_id=author_id,
        )

    async def run_command_job() -> None:
        handled = await dispatch_command(
            cfg,
            command_id=command_id,
            args_text=args_text,
            full_text=full_text,
            channel_id=effective_channel_id,
            message_id=message_id,
            guild_id=guild_id,
            thread_id=thread_id,
            reply_ref=None,  # Slash commands don't have a message to reply to
            reply_text=None,
            running_tasks=running_tasks,
            on_thread_known=on_thread_known,
            default_engine_override=default_engine_override,
            engine_overrides_resolver=engine_overrides_resolver,
        )
        if not handled:
            await cfg.bot.send_message(
                channel_id=effective_channel_id,
                content=f"Command `/{command_id}` not found.",
            )

    asyncio.create_task(
        run_command_job(),
        name=f"tunapi-discord:plugin:{command_id}:{effective_channel_id}",
    )

    # Close the deferred interaction promptly; command output goes to the channel/thread.
    target = f"<#{effective_channel_id}>"
    await ctx.followup.send(f"Started `/{command_id}` in {target}.", ephemeral=True)
