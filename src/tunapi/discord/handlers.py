"""Slash command and message handlers for Discord."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
    resolve_overrides,
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
from .basic_commands import register_basic_commands
from .pref_commands import register_pref_commands
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
    cancel_task: Callable[[int], Awaitable[object]],
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

    register_basic_commands(
        bot,
        state_store=state_store,
        prefs_store=prefs_store,
        get_running_task=get_running_task,
        cancel_task=cancel_task,
        require_allowed_user=require_allowed_user,
        handle_ctx_command=_handle_ctx_command,
    )

    register_pref_commands(
        bot,
        state_store=state_store,
        prefs_store=prefs_store,
        runtime=runtime,
        require_allowed_user=require_allowed_user,
        require_admin=_require_admin,
        trigger_mode_default=trigger_mode_default,
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
