from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import discord

if TYPE_CHECKING:
    from tunapi.runner_bridge import RunningTasks

    from .bridge import DiscordBridgeConfig
    from .client import DiscordBotClient
    from .prefs import DiscordPrefsStore
    from .state import DiscordStateStore


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


def register_engine_commands(
    bot: DiscordBotClient,
    *,
    cfg: DiscordBridgeConfig,
    state_store: DiscordStateStore,
    prefs_store: DiscordPrefsStore,
    running_tasks: RunningTasks,
    handle_engine_command: Any,
) -> list[str]:
    """Register dynamic slash commands for each available engine."""
    from tunapi.logging import get_logger

    logger = get_logger(__name__)
    pycord_bot = bot.bot
    runtime = cfg.runtime

    registered: list[str] = []

    for engine_id in runtime.available_engine_ids():
        cmd_name = engine_id.lower()
        description = f"Use agent: {cmd_name}"

        def make_engine_command(eng_id: str, cmd: str, desc: str):
            @pycord_bot.slash_command(name=cmd, description=desc)
            async def engine_command(
                ctx: discord.ApplicationContext,
                prompt: str = cast(
                    str,
                    discord.Option(description="The prompt to send to the agent"),
                ),
            ) -> None:
                await handle_engine_command(
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


async def handle_engine_command(
    ctx: discord.ApplicationContext,
    *,
    engine_id: str,
    prompt: str,
    cfg: DiscordBridgeConfig,
    state_store: DiscordStateStore,
    prefs_store: DiscordPrefsStore,
    running_tasks: RunningTasks,
    is_user_allowed_fn: Any,
    resolve_overrides_fn: Any,
    discord_module: Any = discord,
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
    if not is_user_allowed_fn(cfg.allowed_user_ids, user_id):
        await ctx.respond("You are not allowed to use this bot.", ephemeral=True)
        return

    await ctx.defer(ephemeral=True)

    guild_id = ctx.guild.id
    channel_id = ctx.channel_id
    if channel_id is None:
        await ctx.followup.send("This command requires a channel.", ephemeral=True)
        return
    thread_id: int | None = None
    created_new_thread = False
    attempted_thread_create = False

    if isinstance(ctx.channel, discord_module.Thread):
        thread_id = channel_id
        if ctx.channel.parent_id:
            channel_id = ctx.channel.parent_id

    if thread_id is None and isinstance(ctx.channel, discord_module.TextChannel):
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

    overrides = await resolve_overrides_fn(
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

    effective_channel_id = thread_id or channel_id

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
