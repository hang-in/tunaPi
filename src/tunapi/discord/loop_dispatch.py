"""Dispatch and message handling functions extracted from Discord event loop."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from tunapi.core.commands import parse_command
from tunapi.logging import get_logger
from tunapi.model import ResumeToken
from tunapi.transport import MessageRef, SendOptions
from tunapi.runners.run_options import EngineRunOptions

from .allowlist import is_user_allowed
from .loop_state import (
    DiscordLoopContext,
    _extract_engine_id_from_header,
    _strip_ctx_lines,
)
from .handlers import (
    extract_prompt_from_message,
    is_bot_mentioned,
    parse_branch_prefix,
    should_process_message,
)
from .overrides import (
    resolve_effective_default_engine,
    resolve_overrides,
    resolve_trigger_mode,
)
from .types import DiscordChannelContext, DiscordThreadContext
from .voice_messages import is_audio_attachment

if TYPE_CHECKING:
    from tunapi.context import RunContext

logger = get_logger(__name__)


async def handle_message(ctx: DiscordLoopContext, message: discord.Message) -> None:
    """Handle an incoming Discord message."""
    logger.debug(
        "message.raw",
        channel_type=type(message.channel).__name__,
        channel_id=message.channel.id,
        author=message.author.name,
        content_preview=message.content[:50] if message.content else "",
    )

    if message.guild is None:
        logger.debug("message.skipped", reason="not in guild (DM)")
        return

    if ctx.startup_cutoff is not None and message.created_at < ctx.startup_cutoff:
        logger.debug(
            "message.skipped",
            reason="startup_backlog",
            created_at=str(message.created_at),
            startup_cutoff=str(ctx.startup_cutoff),
        )
        return

    if not should_process_message(message, ctx.cfg.bot.user, require_mention=False):
        logger.debug("message.skipped", reason="should_process_message returned False")
        return

    author_id = getattr(message.author, "id", None)
    if not isinstance(author_id, int):
        author_id = None
    if not is_user_allowed(ctx.cfg.allowed_user_ids, author_id):
        logger.debug(
            "message.skipped",
            reason="not in allowed_user_ids",
            author_id=author_id,
        )
        return
    files_allowed = is_user_allowed(ctx.cfg.files.allowed_user_ids, author_id)

    channel_id = message.channel.id
    guild_id = message.guild.id
    thread_id = None
    is_new_thread = False

    if ctx.state_store and not isinstance(message.channel, discord.Thread):
        current_startup = await ctx.state_store.get_startup_channel(guild_id)
        if current_startup is None:
            await ctx.state_store.set_startup_channel(guild_id, channel_id)
            logger.info(
                "startup_channel.auto_set",
                guild_id=guild_id,
                channel_id=channel_id,
            )

    if isinstance(message.channel, discord.Thread):
        thread_id = message.channel.id
        parent = message.channel.parent
        if parent:
            channel_id = parent.id
        logger.debug(
            "message.in_thread",
            thread_id=thread_id,
            parent_channel_id=channel_id,
        )
        with contextlib.suppress(discord.HTTPException):
            await message.channel.join()

    channel_context: DiscordChannelContext | None = None
    thread_context: DiscordThreadContext | None = None

    if ctx.state_store and guild_id:
        if thread_id:
            ctx_val = await ctx.state_store.get_context(guild_id, thread_id)
            if isinstance(ctx_val, DiscordThreadContext):
                thread_context = ctx_val

        ctx_val = await ctx.state_store.get_context(guild_id, channel_id)
        if isinstance(ctx_val, DiscordChannelContext):
            channel_context = ctx_val

    trigger_mode = await resolve_trigger_mode(
        ctx.prefs_store,
        guild_id,
        channel_id,
        thread_id,
        default_mode=ctx.cfg.trigger_mode_default,
    )
    if trigger_mode == "mentions":
        bot_mentioned = is_bot_mentioned(message, ctx.cfg.bot.user)
        is_reply_to_bot = False
        if message.reference and message.reference.message_id:
            try:
                ref_msg = await message.channel.fetch_message(
                    message.reference.message_id
                )
                is_reply_to_bot = ref_msg.author == ctx.cfg.bot.user
            except discord.NotFound:
                pass
        if not bot_mentioned and not is_reply_to_bot:
            logger.debug(
                "message.skipped",
                reason="trigger_mode=mentions, bot not mentioned or replied to",
            )
            return

    run_context: RunContext | None = None
    if thread_context:
        from tunapi.context import RunContext

        run_context = RunContext(
            project=thread_context.project,
            branch=thread_context.branch,
        )
    elif channel_context:
        from tunapi.context import RunContext

        run_context = RunContext(
            project=channel_context.project,
            branch=channel_context.worktree_base,
        )

    prompt = extract_prompt_from_message(message, ctx.cfg.bot.user)

    branch_override: str | None = None
    if thread_id is None:
        branch_override, prompt = parse_branch_prefix(prompt)
        if branch_override:
            logger.info("branch.override", branch=branch_override)

    cmd, cmd_args = parse_command(prompt)
    if cmd == "rt":
        import tunapi.discord.loop as loop

        rt_send_opts = SendOptions(thread_id=thread_id)
        await loop._dispatch_rt_command(
            cmd_args,
            channel_id=channel_id,
            thread_id=thread_id,
            cfg=ctx.cfg,
            running_tasks=ctx.running_tasks,
            roundtables=ctx.roundtable_store,
            run_context=run_context,
            send_opts=rt_send_opts,
        )
        return

    attachments_for_files = list(message.attachments)
    audio_attachments = [
        attachment
        for attachment in attachments_for_files
        if is_audio_attachment(attachment)
    ]
    non_audio_attachments = [
        attachment
        for attachment in attachments_for_files
        if not is_audio_attachment(attachment)
    ]

    if audio_attachments and not prompt.strip() and ctx.voice_attachment_transcriber:
        attachment = audio_attachments[0]
        if attachment.size > ctx.cfg.voice_messages.max_bytes:
            await message.reply(
                "Voice message is too large to transcribe.\n"
                f"- Size: {attachment.size} bytes\n"
                f"- Max: {ctx.cfg.voice_messages.max_bytes} bytes",
                mention_author=False,
            )
            return

        try:
            payload = await attachment.read()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "voice_messages.read_failed",
                filename=attachment.filename,
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            return

        transcript = await ctx.voice_attachment_transcriber.transcribe_bytes(
            payload,
            suffix=Path(attachment.filename or "voice.bin").suffix or ".bin",
        )
        if not transcript.strip():
            await message.reply(
                "Couldn't transcribe that voice message.", mention_author=False
            )
            return

        logger.info(
            "voice_messages.transcribed",
            message_id=message.id,
            filename=attachment.filename,
            transcript_length=len(transcript),
        )
        prompt = transcript
        attachments_for_files = non_audio_attachments

    has_attachments = bool(message.attachments)
    if (
        not prompt.strip()
        and not branch_override
        and (not has_attachments or not files_allowed)
    ):
        return

    if (
        audio_attachments
        and not non_audio_attachments
        and not prompt.strip()
        and branch_override is None
        and not ctx.cfg.voice_messages.enabled
    ):
        logger.debug("voice_messages.ignored", reason="disabled")
        return

    if branch_override:
        from tunapi.context import RunContext

        if channel_context:
            run_context = RunContext(
                project=channel_context.project,
                branch=branch_override,
            )
        else:
            logger.warning(
                "branch.no_project",
                branch=branch_override,
                channel_id=channel_id,
            )
            await message.reply(
                f"Cannot use `@{branch_override}` - this channel has no project bound.\n"
                "Use `/bind <project>` first to bind this channel to a project.",
                mention_author=False,
            )
            return

    auto_thread_enabled = not (
        ctx.cfg.session_mode == "chat" and branch_override is None
    )

    if (
        auto_thread_enabled
        and thread_id is None
        and isinstance(message.channel, discord.TextChannel)
    ):
        if branch_override:
            thread_name = branch_override
        else:
            thread_name = prompt[:100] if len(prompt) <= 100 else prompt[:97] + "..."
        created_thread_id = await ctx.cfg.bot.create_thread(
            channel_id=channel_id,
            message_id=message.id,
            name=thread_name,
        )
        if created_thread_id is not None:
            thread_id = created_thread_id
            is_new_thread = True
            logger.info(
                "thread.created",
                channel_id=channel_id,
                thread_id=thread_id,
                name=thread_name,
            )

            if branch_override and ctx.state_store and guild_id and channel_context:
                new_thread_context = DiscordThreadContext(
                    project=channel_context.project,
                    branch=branch_override,
                    worktrees_dir=channel_context.worktrees_dir,
                    default_engine=channel_context.default_engine,
                )
                await ctx.state_store.set_context(
                    guild_id, thread_id, new_thread_context
                )
                logger.info(
                    "thread.context_saved",
                    thread_id=thread_id,
                    project=channel_context.project,
                    branch=branch_override,
                )

                if not prompt.strip():
                    thread_channel = ctx.cfg.bot.bot.get_channel(thread_id)
                    if thread_channel and isinstance(thread_channel, discord.Thread):
                        await thread_channel.send(
                            f"Thread bound to branch `{branch_override}`. "
                            "Send a message here to start prompting."
                        )
                    logger.info(
                        "branch.thread_only",
                        thread_id=thread_id,
                        branch=branch_override,
                    )
                    return

    resume_token: ResumeToken | None = None
    session_key = thread_id if thread_id else channel_id
    author_id = getattr(message.author, "id", None)
    if not isinstance(author_id, int):
        author_id = None
    logger.debug(
        "session.lookup",
        guild_id=guild_id,
        session_key=session_key,
        author_id=author_id,
        has_state_store=ctx.state_store is not None,
    )

    engine_id, engine_source = await resolve_effective_default_engine(
        ctx.prefs_store,
        guild_id=guild_id,
        channel_id=channel_id,
        thread_id=thread_id,
        bound_thread_default=thread_context.default_engine if thread_context else None,
        bound_channel_default=channel_context.default_engine
        if channel_context
        else None,
        config_default=ctx.cfg.runtime.default_engine,
    )
    if engine_id is None:
        engine_id = ctx.cfg.runtime.default_engine or "claude"
    logger.debug(
        "engine.resolved",
        engine_id=engine_id,
        source=engine_source,
        channel_id=channel_id,
        thread_id=thread_id,
    )

    reply_text: str | None = None

    if message.reference and message.reference.message_id:
        ref_msg: discord.Message | None = None
        resolved = getattr(message.reference, "resolved", None)
        if isinstance(resolved, discord.Message):
            ref_msg = resolved
        else:
            with contextlib.suppress(discord.NotFound, discord.HTTPException):
                ref_msg = await message.channel.fetch_message(
                    message.reference.message_id
                )

        if (
            ref_msg is not None
            and ctx.cfg.bot.user is not None
            and ref_msg.author == ctx.cfg.bot.user
        ):
            reply_text = _strip_ctx_lines(ref_msg.content)
            inferred = _extract_engine_id_from_header(ref_msg.content)
            if inferred and inferred in ctx.cfg.runtime.engine_ids:
                engine_id = inferred
                logger.debug(
                    "engine.inferred_from_reply",
                    engine_id=engine_id,
                    ref_message_id=ref_msg.id,
                )

    try:
        resolved_msg = ctx.cfg.runtime.resolve_message(
            text=prompt,
            reply_text=reply_text,
            ambient_context=run_context,
            chat_id=session_key,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "resume.resolve_failed",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        resolved_msg = None

    if resolved_msg is not None and resolved_msg.resume_token is not None:
        resume_token = resolved_msg.resume_token
        engine_id = resume_token.engine
        logger.debug(
            "resume.extracted_from_reply",
            engine_id=engine_id,
            source="reply" if reply_text else "message",
        )

    import tunapi.discord.loop as loop

    if (
        resume_token is None
        and ctx.state_store
        and guild_id
        and ctx.cfg.session_mode == "chat"
    ):
        token_str = await ctx.state_store.get_session(
            guild_id,
            session_key,
            engine_id,
            author_id=author_id,
        )
        if token_str:
            resume_token = ResumeToken(engine=engine_id, value=token_str)
            logger.info(
                "session.restored",
                guild_id=guild_id,
                session_key=session_key,
                author_id=author_id,
                engine_id=engine_id,
                token_preview=token_str[:20] + "..."
                if len(token_str) > 20
                else token_str,
            )
        else:
            logger.debug(
                "session.not_found",
                guild_id=guild_id,
                session_key=session_key,
                author_id=author_id,
                engine_id=engine_id,
            )

    reply_ref: MessageRef | None = None
    if not is_new_thread:
        reply_ref = MessageRef(
            channel_id=channel_id,
            message_id=message.id,
            thread_id=thread_id,
        )

    logger.info(
        "message.received",
        channel_id=channel_id,
        thread_id=thread_id,
        session_key=session_key,
        message_id=message.id,
        author=message.author.name,
        prompt_length=len(prompt),
        has_context=run_context is not None,
        is_new_thread=is_new_thread,
        has_resume_token=resume_token is not None,
    )

    if (
        ctx.media_buffer is not None
        and isinstance(message.channel, discord.Thread)
        and thread_id is not None
        and author_id is not None
        and ctx.cfg.files.enabled
        and ctx.cfg.files.auto_put
        and files_allowed
        and run_context is not None
        and run_context.project is not None
    ):
        if attachments_for_files and not prompt.strip():
            ctx.media_buffer.add(
                message,
                prompt="",
                guild_id=guild_id,
                channel_id=channel_id,
                thread_id=thread_id,
                job_channel_id=thread_id,
                engine_id=engine_id,
                resume_token=resume_token,
                context=run_context,
            )
            logger.debug(
                "media_group.buffered",
                thread_id=thread_id,
                author_id=author_id,
                message_id=message.id,
                attachment_count=len(attachments_for_files),
            )
            return
        if (
            prompt.strip()
            and not attachments_for_files
            and ctx.media_buffer.has_pending(channel_id=thread_id, author_id=author_id)
        ):
            ctx.media_buffer.add(
                message,
                prompt=prompt,
                guild_id=guild_id,
                channel_id=channel_id,
                thread_id=thread_id,
                job_channel_id=thread_id,
                engine_id=engine_id,
                resume_token=resume_token,
                context=run_context,
            )
            logger.debug(
                "media_group.prompt_attached",
                thread_id=thread_id,
                author_id=author_id,
                message_id=message.id,
                prompt_length=len(prompt),
            )
            return

    logger.debug(
        "auto_put.check",
        files_enabled=ctx.cfg.files.enabled,
        auto_put=ctx.cfg.files.auto_put,
        attachment_count=len(attachments_for_files),
    )
    if (
        ctx.cfg.files.enabled
        and ctx.cfg.files.auto_put
        and attachments_for_files
        and files_allowed
    ):
        from tunapi.config import ConfigError
        from .file_transfer import format_bytes, save_attachment

        if run_context is None or run_context.project is None:
            logger.debug(
                "auto_put.skipped",
                reason="no project context",
                attachment_count=len(attachments_for_files),
            )
        else:
            try:
                run_root = ctx.cfg.runtime.resolve_run_cwd(run_context)
            except ConfigError as exc:
                logger.warning("auto_put.cwd_error", error=str(exc))
                run_root = None

            if run_root is not None:
                file_annotations: list[str] = []
                saved_files: list[str] = []

                for attachment in attachments_for_files:
                    result = await save_attachment(
                        attachment,
                        run_root,
                        ctx.cfg.files.uploads_dir,
                        ctx.cfg.files.deny_globs,
                        max_bytes=ctx.cfg.files.max_upload_bytes,
                    )
                    if result.error is not None:
                        logger.warning(
                            "auto_put.failed",
                            filename=attachment.filename,
                            error=result.error,
                        )
                    elif result.rel_path is not None and result.size is not None:
                        logger.info(
                            "auto_put.saved",
                            filename=attachment.filename,
                            rel_path=result.rel_path.as_posix(),
                            size=result.size,
                        )
                        file_annotations.append(
                            f"[uploaded file: {result.rel_path.as_posix()}]"
                        )
                        saved_files.append(
                            f"`{result.rel_path.as_posix()}` ({format_bytes(result.size)})"
                        )

                if ctx.cfg.files.auto_put_mode == "prompt" and file_annotations:
                    prompt = "\n".join(file_annotations) + "\n\n" + prompt
                    logger.debug(
                        "auto_put.annotated",
                        annotation_count=len(file_annotations),
                    )
                elif ctx.cfg.files.auto_put_mode == "upload" and saved_files:
                    if not prompt.strip():
                        confirm_msg = "saved " + ", ".join(saved_files)
                        await message.reply(confirm_msg, mention_author=False)
                        return

    job_channel_id = thread_id if thread_id else channel_id

    session_meta: tuple[int, int | None] | tuple[int, int | None, int] = (
        (guild_id, channel_id, author_id)
        if author_id is not None
        else (guild_id, channel_id)
    )
    if ctx.resume_resolver is not None:
        reply_id = (
            message.reference.message_id
            if message.reference and message.reference.message_id
            else None
        )
        decision = await ctx.resume_resolver.resolve(
            resume_token=resume_token,
            reply_id=reply_id,
            chat_id=job_channel_id,
            user_msg_id=message.id,
            thread_id=thread_id,
            session_key=session_meta,
            prompt_text=prompt,
        )
        if decision.handled_by_running_task:
            return
        resume_token = decision.resume_token

    if resume_token is not None and ctx.scheduler is not None:
        progress_ref = await loop._send_queued_progress(
            ctx.cfg,
            channel_id=job_channel_id,
            user_msg_id=message.id,
            thread_id=thread_id,
            resume_token=resume_token,
            context=run_context,
        )
        await ctx.scheduler.enqueue_resume(
            job_channel_id,
            message.id,
            prompt,
            resume_token,
            run_context,
            thread_id,
            session_meta,
            progress_ref,
        )
        return

    overrides = await resolve_overrides(
        ctx.prefs_store, guild_id, channel_id, thread_id, engine_id
    )
    run_options: EngineRunOptions | None = None
    if overrides.model or overrides.reasoning:
        run_options = EngineRunOptions(
            model=overrides.model,
            reasoning=overrides.reasoning,
        )
        logger.debug(
            "run_options.resolved",
            model=overrides.model,
            model_source=overrides.source_model,
            reasoning=overrides.reasoning,
            reasoning_source=overrides.source_reasoning,
        )

    try:
        await loop.run_job(
            ctx,
            channel_id=job_channel_id,
            user_msg_id=message.id,
            text=prompt,
            resume_token=resume_token,
            context=run_context,
            engine_id=engine_id,
            author_id=author_id,
            thread_id=thread_id,
            reply_ref=reply_ref,
            guild_id=guild_id,
            run_options=run_options,
        )
    except Exception:
        logger.exception("handle_message.run_job_failed")
