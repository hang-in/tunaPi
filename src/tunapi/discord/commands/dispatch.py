"""Command dispatch for Discord plugin commands."""

from __future__ import annotations

import shlex
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import anyio

from tunapi.commands import CommandContext, get_command
from tunapi.config import ConfigError
from tunapi.logging import get_logger
from tunapi.model import EngineId, ResumeToken
from tunapi.runner_bridge import RunningTasks
from tunapi.runners.run_options import EngineRunOptions
from tunapi.transport import MessageRef

from .executor import _DiscordCommandExecutor

if TYPE_CHECKING:
    from ..bridge import DiscordBridgeConfig

logger = get_logger(__name__)


def split_command_args(text: str) -> tuple[str, ...]:
    """Split command arguments, handling quotes."""
    if not text.strip():
        return ()
    try:
        return tuple(shlex.split(text))
    except ValueError:
        return tuple(text.split())


async def dispatch_command(
    cfg: DiscordBridgeConfig,
    *,
    command_id: str,
    args_text: str,
    full_text: str,
    channel_id: int,
    message_id: int,
    guild_id: int | None,
    thread_id: int | None,
    reply_ref: MessageRef | None,
    reply_text: str | None,
    running_tasks: RunningTasks,
    on_thread_known: Callable[[ResumeToken, anyio.Event], Awaitable[None]] | None,
    default_engine_override: EngineId | None,
    engine_overrides_resolver: Callable[[EngineId], Awaitable[EngineRunOptions | None]]
    | None,
) -> bool:
    """Dispatch a command to its plugin backend.

    Returns True if the command was handled, False if no plugin was found.
    """
    allowlist = cfg.runtime.allowlist

    executor = _DiscordCommandExecutor(
        exec_cfg=cfg.exec_cfg,
        runtime=cfg.runtime,
        running_tasks=running_tasks,
        on_thread_known=on_thread_known,
        engine_overrides_resolver=engine_overrides_resolver,
        channel_id=channel_id,
        user_msg_id=message_id,
        thread_id=thread_id,
        guild_id=guild_id,
        show_resume_line=cfg.show_resume_line,
        default_engine_override=default_engine_override,
    )

    message_ref = MessageRef(
        channel_id=channel_id,
        message_id=message_id,
        thread_id=thread_id,
    )

    try:
        backend = get_command(command_id, allowlist=allowlist, required=False)
    except ConfigError as exc:
        await executor.send(f"error:\n{exc}", reply_to=message_ref, notify=True)
        return True

    if backend is None:
        return False

    try:
        plugin_config = cfg.runtime.plugin_config(command_id)
    except ConfigError as exc:
        await executor.send(f"error:\n{exc}", reply_to=message_ref, notify=True)
        return True

    ctx = CommandContext(
        command=command_id,
        text=full_text,
        args_text=args_text,
        args=split_command_args(args_text),
        message=message_ref,
        reply_to=reply_ref,
        reply_text=reply_text,
        config_path=cfg.runtime.config_path,
        plugin_config=plugin_config,
        runtime=cfg.runtime,
        executor=executor,
    )

    try:
        result = await backend.handle(ctx)
    except Exception as exc:
        logger.exception(
            "command.failed",
            command=command_id,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        await executor.send(f"error:\n{exc}", reply_to=message_ref, notify=True)
        return True

    if result is not None:
        reply_to = message_ref if result.reply_to is None else result.reply_to
        await executor.send(result.text, reply_to=reply_to, notify=result.notify)

    return True
