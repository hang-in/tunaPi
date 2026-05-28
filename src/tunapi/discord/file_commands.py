from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

import discord

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

if TYPE_CHECKING:
    from tunapi.transport_runtime import TransportRuntime

    from .bridge import DiscordFilesSettings
    from .state import DiscordStateStore


def register_file_command(
    pycord_bot,
    *,
    files: DiscordFilesSettings,
    runtime: TransportRuntime,
    state_store: DiscordStateStore,
    require_allowed_user: Callable[..., Awaitable[bool]],
    require_admin: Callable[[discord.ApplicationContext], Awaitable[bool]],
    discord_module=discord,
) -> None:
    """Register the Discord /file command."""

    async def _get_project_root(
        ctx: discord.ApplicationContext,
    ) -> tuple[Path | None, RunContext | None]:
        if ctx.guild is None:
            return None, None

        guild_id = ctx.guild.id
        channel_id = ctx.channel_id
        if channel_id is None:
            return None, None

        context = None
        channel = ctx.channel

        if isinstance(channel, discord_module.Thread):
            context = await state_store.get_context(guild_id, channel.id)
            if context is None and channel.parent_id:
                context = await state_store.get_context(guild_id, channel.parent_id)
        else:
            context = await state_store.get_context(guild_id, channel_id)

        if context is None:
            return None, None

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

        try:
            run_root = runtime.resolve_run_cwd(run_context)
        except ConfigError:
            return None, None

        return run_root, run_context

    @pycord_bot.slash_command(name="file", description="Upload or download files")
    async def file_command(
        ctx: discord.ApplicationContext,
        action: str = cast(
            str,
            discord.Option(
                description="Action: get (download) or put (upload)",
                choices=["get", "put"],
            ),
        ),
        path: str = cast(
            str,
            discord.Option(
                description="File path relative to project directory",
            ),
        ),
        force: bool = cast(
            bool,
            discord.Option(
                default=False,
                description="Overwrite existing files (put only)",
            ),
        ),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not await require_allowed_user(ctx, require_files=True):
            return

        if not await require_admin(ctx):
            return

        project_root, _run_context = await _get_project_root(ctx)
        if project_root is None:
            await ctx.respond(
                "This channel is not bound to a project.\n"
                "Use `/bind <project>` first to enable file transfers.",
                ephemeral=True,
            )
            return

        deny_globs = files.deny_globs

        if action == "get":
            rel_path = normalize_relative_path(path)
            if rel_path is None:
                await ctx.respond(
                    "Invalid path. Must be relative, no `..` or `.git`.",
                    ephemeral=True,
                )
                return

            denied = deny_reason(rel_path, deny_globs)
            if denied:
                await ctx.respond(f"Path denied by rule: `{denied}`", ephemeral=True)
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
                    file = discord_module.File(
                        fp=__import__("io").BytesIO(zip_data),
                        filename=filename,
                    )
                    await ctx.followup.send(
                        f"Directory `{rel_path}` ({format_bytes(len(zip_data))})",
                        file=file,
                        ephemeral=True,
                    )
                else:
                    size = target.stat().st_size
                    if size > MAX_FILE_SIZE:
                        await ctx.followup.send(
                            f"File too large ({format_bytes(size)} > {format_bytes(MAX_FILE_SIZE)}).",
                            ephemeral=True,
                        )
                        return
                    file = discord_module.File(fp=str(target), filename=target.name)
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

            attachments: list[discord.Attachment] = []
            ref_id: int | None = None
            if ctx.message is not None:
                attachments = list(ctx.message.attachments or [])
                if ctx.message.reference and ctx.message.reference.message_id:
                    ref_id = ctx.message.reference.message_id

            if not attachments and ref_id is not None:
                fetch_message = getattr(ctx.channel, "fetch_message", None)
                try:
                    ref_msg = (
                        await fetch_message(ref_id) if callable(fetch_message) else None
                    )
                except (discord_module.NotFound, discord_module.HTTPException):
                    ref_msg = None
                if ref_msg is not None:
                    attachments = list(ref_msg.attachments or [])

            if not attachments:
                await ctx.respond(
                    "Attach file(s) to this command, or reply to a message with attachments and run `/file put`.",
                    ephemeral=True,
                )
                return

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
