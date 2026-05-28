from __future__ import annotations

import logging
from typing import Any, cast
import anyio

from tunapi.model import ResumeToken
from tunapi.runners.run_options import EngineRunOptions, apply_run_options
from tunapi.transport import MessageRef, RenderedMessage, SendOptions
from .loop_state import DiscordLoopContext, _MediaGroupState
from .overrides import resolve_overrides
from .render import prepare_discord
from tunapi.markdown import MarkdownParts

logger = get_logger = lambda name: logging.getLogger(name)


async def run_job(
    ctx: DiscordLoopContext,
    channel_id: int,
    user_msg_id: int,
    text: str,
    resume_token: ResumeToken | None,
    context: Any,
    engine_id: str | None,
    author_id: int | None = None,
    thread_id: int | None = None,
    reply_ref: MessageRef | None = None,
    guild_id: int | None = None,
    run_options: EngineRunOptions | None = None,
    progress_ref: MessageRef | None = None,
) -> None:
    """Run an engine job."""
    from tunapi.config import ConfigError
    from tunapi.logging import bind_run_context, clear_context
    from tunapi.runner_bridge import IncomingMessage
    from tunapi.runner_bridge import handle_message as tunapi_handle_message
    from tunapi.utils.paths import reset_run_base_dir, set_run_base_dir
    import tunapi.discord.loop as loop

    log = logger("tunapi.discord.job_handlers")
    log.info(
        "run_job.start",
        channel_id=channel_id,
        user_msg_id=user_msg_id,
        text_length=len(text),
        has_context=context is not None,
        project=context.project if context else None,
        branch=context.branch if context else None,
    )

    try:
        # Resolve the runner
        resolved = ctx.cfg.runtime.resolve_runner(
            resume_token=resume_token,
            engine_override=ctx.default_engine_override or engine_id,
        )
        if not resolved.available:
            log.error(
                "run_job.runner_unavailable",
                engine=resolved.engine,
                issue=resolved.issue,
            )
            return

        # Resolve working directory
        try:
            cwd = ctx.cfg.runtime.resolve_run_cwd(context)
        except ConfigError as exc:
            log.error("run_job.cwd_error", error=str(exc))
            return

        run_base_token = set_run_base_dir(cwd)
        try:
            # Bind logging context
            run_fields = {
                "chat_id": channel_id,
                "user_msg_id": user_msg_id,
                "engine": resolved.runner.engine,
                "resume": resume_token.value if resume_token else None,
            }
            if context is not None:
                run_fields["project"] = context.project
                run_fields["branch"] = context.branch
            if cwd is not None:
                run_fields["cwd"] = str(cwd)
            bind_run_context(**run_fields)

            # Build incoming message
            incoming = IncomingMessage(
                channel_id=channel_id,
                message_id=user_msg_id,
                text=text,
                reply_to=reply_ref,
                thread_id=thread_id,
            )

            # Build context line if we have context
            context_line = ctx.cfg.runtime.format_context_line(context)

            # Callback to save the resume token when it becomes known
            async def on_thread_known(
                new_token: ResumeToken, done: anyio.Event
            ) -> None:
                log.debug(
                    "on_thread_known.called",
                    guild_id=guild_id,
                    channel_id=channel_id,
                    thread_id=thread_id,
                    token_preview=new_token.value[:20] + "..."
                    if len(new_token.value) > 20
                    else new_token.value,
                )
                save_key = thread_id if thread_id else channel_id
                await loop._save_session_token(
                    state_store=ctx.state_store,
                    guild_id=guild_id,
                    session_key=save_key,
                    author_id=author_id,
                    token=new_token,
                )
                if ctx.state_store and guild_id:
                    log.info(
                        "session.saved",
                        guild_id=guild_id,
                        session_key=save_key,
                        author_id=author_id,
                        engine_id=new_token.engine,
                    )
                else:
                    log.debug(
                        "on_thread_known.not_saving",
                        has_state_store=ctx.state_store is not None,
                        guild_id=guild_id,
                    )
                if ctx.scheduler is not None:
                    await ctx.scheduler.note_thread_known(new_token, done)

            with apply_run_options(run_options):
                await tunapi_handle_message(
                    ctx.cfg.exec_cfg,
                    runner=resolved.runner,
                    incoming=incoming,
                    resume_token=resume_token,
                    context=context,
                    context_line=context_line,
                    strip_resume_line=ctx.cfg.runtime.is_resume_line,
                    running_tasks=ctx.running_tasks,
                    on_thread_known=on_thread_known,
                    progress_ref=progress_ref,
                )
            log.info("run_job.complete", channel_id=channel_id)
        finally:
            reset_run_base_dir(run_base_token)
    except Exception:
        log.exception("run_job.error", channel_id=channel_id)
    finally:
        clear_context()


async def run_thread_job(ctx: DiscordLoopContext, job: Any) -> None:
    guild_id: int | None = None
    parent_channel_id: int | None = None
    author_id: int | None = None
    if job.session_key is not None:
        guild_id = job.session_key[0]
        parent_channel_id = job.session_key[1]
        if len(job.session_key) >= 3 and isinstance(job.session_key[2], int):
            author_id = job.session_key[2]

    engine_id = job.resume_token.engine
    run_options: EngineRunOptions | None = None
    if guild_id is not None and ctx.prefs_store is not None:
        overrides = await resolve_overrides(
            ctx.prefs_store,
            guild_id,
            parent_channel_id or cast(int, job.chat_id),
            cast(int | None, job.thread_id),
            engine_id,
        )
        if overrides.model or overrides.reasoning:
            run_options = EngineRunOptions(
                model=overrides.model,
                reasoning=overrides.reasoning,
            )

    import tunapi.discord.loop as loop

    await loop.run_job(
        ctx,
        channel_id=cast(int, job.chat_id),
        user_msg_id=cast(int, job.user_msg_id),
        text=job.text,
        resume_token=job.resume_token,
        context=job.context,
        engine_id=engine_id,
        author_id=author_id,
        thread_id=cast(int | None, job.thread_id),
        reply_ref=None,
        guild_id=guild_id,
        run_options=run_options,
        progress_ref=job.progress_ref,
    )


async def dispatch_media_group(
    ctx: DiscordLoopContext, state: _MediaGroupState
) -> None:
    log = logger("tunapi.discord.job_handlers")
    if not state.items:
        return
    if (
        state.guild_id is None
        or state.channel_id is None
        or state.job_channel_id is None
    ):
        return
    if state.context is None or state.context.project is None:
        return

    ordered = sorted(state.items, key=lambda item: item.message.id)
    command_item = next(
        (item for item in ordered if item.prompt.strip()),
        ordered[-1],
    )
    prompt_text = command_item.prompt.strip()

    from tunapi.config import ConfigError
    from .file_transfer import format_bytes, save_attachment

    try:
        run_root = ctx.cfg.runtime.resolve_run_cwd(state.context)
    except ConfigError as exc:
        log.warning("media_group.cwd_error", error=str(exc))
        return
    if run_root is None:
        return

    file_annotations: list[str] = []
    saved_files: list[str] = []
    failures: list[str] = []

    for item in ordered:
        for attachment in item.message.attachments:
            result = await save_attachment(
                attachment,
                run_root,
                ctx.cfg.files.uploads_dir,
                ctx.cfg.files.deny_globs,
                max_bytes=ctx.cfg.files.max_upload_bytes,
            )
            if result.error is not None:
                failures.append(f"`{attachment.filename}` ({result.error})")
                continue
            if result.rel_path is None or result.size is None:
                continue
            file_annotations.append(f"[uploaded file: {result.rel_path.as_posix()}]")
            saved_files.append(
                f"`{result.rel_path.as_posix()}` ({format_bytes(result.size)})"
            )

    if not saved_files:
        return

    if not prompt_text:
        parts = MarkdownParts(header="saved " + ", ".join(saved_files))
        text = prepare_discord(parts)
        reply_to = MessageRef(
            channel_id=state.job_channel_id,
            message_id=command_item.message.id,
            thread_id=state.thread_id,
        )
        await ctx.cfg.exec_cfg.transport.send(
            channel_id=state.job_channel_id,
            message=RenderedMessage(text=text, extra={"show_cancel": False}),
            options=SendOptions(
                reply_to=reply_to, notify=False, thread_id=state.thread_id
            ),
        )
        return

    combined_prompt = prompt_text
    if ctx.cfg.files.auto_put_mode == "prompt" and file_annotations:
        combined_prompt = "\n".join(file_annotations) + "\n\n" + prompt_text

    engine_id = state.engine_id or (ctx.cfg.runtime.default_engine or "claude")
    overrides = await resolve_overrides(
        ctx.prefs_store,
        state.guild_id,
        state.channel_id,
        state.thread_id,
        engine_id,
    )
    run_options: EngineRunOptions | None = None
    if overrides.model or overrides.reasoning:
        run_options = EngineRunOptions(
            model=overrides.model,
            reasoning=overrides.reasoning,
        )

    if failures:
        combined_prompt = (
            "[upload failures]\n"
            + "\n".join(f"- {item}" for item in failures)
            + "\n\n"
            + combined_prompt
        )

    import tunapi.discord.loop as loop

    await loop.run_job(
        ctx,
        channel_id=state.job_channel_id,
        user_msg_id=command_item.message.id,
        text=combined_prompt,
        resume_token=state.resume_token,
        context=state.context,
        engine_id=engine_id,
        author_id=state.author_id,
        thread_id=state.thread_id,
        reply_ref=None,
        guild_id=state.guild_id,
        run_options=run_options,
    )
