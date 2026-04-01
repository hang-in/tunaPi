"""Main event loop for Discord transport."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import discord

from tunapi.config_watch import ConfigReload, watch_config as watch_config_changes
from tunapi.logging import get_logger
from tunapi.markdown import MarkdownParts
from tunapi.model import ResumeToken
from tunapi.progress import ProgressTracker
from tunapi.runner_bridge import RunningTasks
from tunapi.scheduler import ThreadJob, ThreadScheduler
from tunapi.runners.run_options import EngineRunOptions, apply_run_options
from tunapi.transport import MessageRef, RenderedMessage, SendOptions

from .allowlist import is_user_allowed
from .loop_state import (
    MediaGroupBuffer,
    ResumeDecision,
    _MediaGroupState,
    _MediaItem,
    _diff_keys,
    _extract_engine_id_from_header,
    _strip_ctx_lines,
)
from .bridge import CANCEL_BUTTON_ID, DiscordBridgeConfig, DiscordTransport
from .commands import discover_command_ids, register_plugin_commands
from .handlers import (
    extract_prompt_from_message,
    is_bot_mentioned,
    parse_branch_prefix,
    register_engine_commands,
    register_slash_commands,
    should_process_message,
)
from .overrides import (
    resolve_effective_default_engine,
    resolve_overrides,
    resolve_trigger_mode,
)
from .prefs import DiscordPrefsStore
from .render import prepare_discord
from .state import DiscordStateStore
from .types import DiscordChannelContext, DiscordThreadContext
from .voice_messages import WhisperAttachmentTranscriber, is_audio_attachment

if TYPE_CHECKING:
    from tunapi.context import RunContext

logger = get_logger(__name__)

__all__ = ["run_main_loop"]


async def _send_startup(cfg: DiscordBridgeConfig, channel_id: int) -> None:
    """Send startup message to the specified channel."""
    logger.debug("startup.message", text=cfg.startup_msg)
    parts = MarkdownParts(header=cfg.startup_msg)
    text = prepare_discord(parts)
    message = RenderedMessage(text=text, extra={})
    sent = await cfg.exec_cfg.transport.send(
        channel_id=channel_id,
        message=message,
    )
    if sent is not None:
        logger.info("startup.sent", channel_id=channel_id)


async def _save_session_token(
    *,
    state_store: DiscordStateStore | None,
    guild_id: int | None,
    session_key: int,
    author_id: int | None,
    token: ResumeToken,
) -> None:
    if state_store is None or guild_id is None:
        return
    await state_store.set_session(
        guild_id, session_key, token.engine, token.value, author_id=author_id
    )


async def _wait_for_resume(running_task) -> ResumeToken | None:
    if running_task.resume is not None:
        return running_task.resume
    resume: ResumeToken | None = None

    async with anyio.create_task_group() as tg:

        async def wait_resume() -> None:
            nonlocal resume
            await running_task.resume_ready.wait()
            resume = running_task.resume
            tg.cancel_scope.cancel()

        async def wait_done() -> None:
            await running_task.done.wait()
            tg.cancel_scope.cancel()

        tg.start_soon(wait_resume)
        tg.start_soon(wait_done)

    return resume


async def _send_plain_reply(
    cfg: DiscordBridgeConfig,
    *,
    channel_id: int,
    user_msg_id: int,
    thread_id: int | None,
    text: str,
) -> None:
    parts = MarkdownParts(header=text)
    rendered_text = prepare_discord(parts)
    reply_ref = MessageRef(
        channel_id=channel_id,
        message_id=user_msg_id,
        thread_id=thread_id,
    )
    await cfg.exec_cfg.transport.send(
        channel_id=channel_id,
        message=RenderedMessage(text=rendered_text, extra={"show_cancel": False}),
        options=SendOptions(reply_to=reply_ref, notify=False, thread_id=thread_id),
    )


async def _send_queued_progress(
    cfg: DiscordBridgeConfig,
    *,
    channel_id: int,
    user_msg_id: int,
    thread_id: int | None,
    resume_token: ResumeToken,
    context: RunContext | None,
) -> MessageRef | None:
    tracker = ProgressTracker(engine=resume_token.engine)
    tracker.set_resume(resume_token)
    context_line = cfg.runtime.format_context_line(context)
    state = tracker.snapshot(context_line=context_line)
    queued = cfg.exec_cfg.presenter.render_progress(
        state,
        elapsed_s=0.0,
        label="queued",
    )
    message = RenderedMessage(
        text=queued.text,
        extra={**queued.extra, "show_cancel": False},
    )
    reply_ref = MessageRef(
        channel_id=channel_id,
        message_id=user_msg_id,
        thread_id=thread_id,
    )
    return await cfg.exec_cfg.transport.send(
        channel_id=channel_id,
        message=message,
        options=SendOptions(reply_to=reply_ref, notify=False, thread_id=thread_id),
    )


async def send_with_resume(
    cfg: DiscordBridgeConfig,
    enqueue: Callable[
        [
            int,
            int,
            str,
            ResumeToken,
            RunContext | None,
            int | None,
            tuple[int, int | None] | None,
            MessageRef | None,
        ],
        Awaitable[None],
    ],
    running_task,
    channel_id: int,
    user_msg_id: int,
    thread_id: int | None,
    session_key: tuple[int, int | None] | None,
    text: str,
) -> None:
    resume = await _wait_for_resume(running_task)
    if resume is None:
        await _send_plain_reply(
            cfg,
            channel_id=channel_id,
            user_msg_id=user_msg_id,
            thread_id=thread_id,
            text="resume token not ready yet; try replying to the final message.",
        )
        return
    progress_ref = await _send_queued_progress(
        cfg,
        channel_id=channel_id,
        user_msg_id=user_msg_id,
        thread_id=thread_id,
        resume_token=resume,
        context=running_task.context,
    )
    await enqueue(
        channel_id,
        user_msg_id,
        text,
        resume,
        running_task.context,
        thread_id,
        session_key,
        progress_ref,
    )


class ResumeResolver:
    def __init__(
        self,
        *,
        cfg: DiscordBridgeConfig,
        task_group,
        running_tasks: Mapping[MessageRef, object],
        enqueue_resume: Callable[
            [
                int,
                int,
                str,
                ResumeToken,
                RunContext | None,
                int | None,
                tuple[int, int | None] | None,
                MessageRef | None,
            ],
            Awaitable[None],
        ],
    ) -> None:
        self._cfg = cfg
        self._task_group = task_group
        self._running_tasks = running_tasks
        self._enqueue_resume = enqueue_resume

    async def resolve(
        self,
        *,
        resume_token: ResumeToken | None,
        reply_id: int | None,
        chat_id: int,
        user_msg_id: int,
        thread_id: int | None,
        session_key: tuple[int, int | None] | None,
        prompt_text: str,
    ) -> ResumeDecision:
        if resume_token is not None:
            return ResumeDecision(
                resume_token=resume_token,
                handled_by_running_task=False,
            )
        if reply_id is not None:
            running_task = self._running_tasks.get(
                MessageRef(channel_id=chat_id, message_id=reply_id)
            )
            if running_task is not None:
                self._task_group.start_soon(
                    send_with_resume,
                    self._cfg,
                    self._enqueue_resume,
                    running_task,
                    chat_id,
                    user_msg_id,
                    thread_id,
                    session_key,
                    prompt_text,
                )
                return ResumeDecision(resume_token=None, handled_by_running_task=True)
        return ResumeDecision(resume_token=None, handled_by_running_task=False)


async def run_main_loop(
    cfg: DiscordBridgeConfig,
    *,
    default_engine_override: str | None = None,
    config_path: Path | None = None,
    transport_config: dict[str, Any] | None = None,
) -> None:
    """Run the main Discord event loop."""
    startup_cutoff = datetime.now(UTC)
    running_tasks: RunningTasks = {}
    state_store = DiscordStateStore(cfg.runtime.config_path)
    prefs_store = DiscordPrefsStore(cfg.runtime.config_path)
    await prefs_store.ensure_loaded()
    _ = cast(DiscordTransport, cfg.exec_cfg.transport)  # Used for type checking only
    scheduler: ThreadScheduler | None = None
    resume_resolver: ResumeResolver | None = None
    media_buffer: MediaGroupBuffer | None = None

    # Initialize voice manager if OpenAI API key is available (needed for TTS)
    # STT uses local Whisper via pywhispercpp
    voice_manager = None
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if openai_api_key:
        try:
            from openai import AsyncOpenAI

            from .voice import WHISPER_MODEL, VoiceManager

            openai_client = AsyncOpenAI(api_key=openai_api_key)
            whisper_model = os.environ.get("WHISPER_MODEL", WHISPER_MODEL)
            voice_manager = VoiceManager(
                cfg.bot,
                openai_client,
                whisper_model=whisper_model,
                allowed_user_ids=cfg.allowed_user_ids,
            )
            logger.info("voice.enabled", whisper_model=whisper_model)
        except ImportError as e:
            logger.warning("voice.disabled", reason=f"missing package: {e}")
    else:
        logger.info("voice.disabled", reason="OPENAI_API_KEY not set (needed for TTS)")

    voice_attachment_transcriber: WhisperAttachmentTranscriber | None = None
    if cfg.voice_messages.enabled:
        whisper_model = os.environ.get(
            "WHISPER_MODEL", cfg.voice_messages.whisper_model
        )
        voice_attachment_transcriber = WhisperAttachmentTranscriber(whisper_model)
        logger.info(
            "voice_messages.enabled",
            whisper_model=whisper_model,
            max_bytes=cfg.voice_messages.max_bytes,
        )

    logger.info(
        "loop.config",
        has_state_store=state_store is not None,
        guild_id=cfg.guild_id,
        voice_enabled=voice_manager is not None,
        voice_messages_enabled=cfg.voice_messages.enabled,
    )

    def get_running_task(channel_id: int) -> int | None:
        """Get the message ID of a running task in a channel."""
        for ref in running_tasks:
            # ref is a MessageRef; check both channel_id and thread_id
            if ref.channel_id == channel_id or ref.thread_id == channel_id:
                return ref.message_id
        return None

    async def cancel_task(channel_id: int) -> None:
        """Cancel a running task in a channel."""
        for ref, task in list(running_tasks.items()):
            # ref is a MessageRef; check both channel_id and thread_id
            if ref.channel_id == channel_id or ref.thread_id == channel_id:
                task.cancel_requested.set()
                break

    # Register built-in slash commands (reserved commands)
    register_slash_commands(
        cfg.bot,
        state_store=state_store,
        prefs_store=prefs_store,
        get_running_task=get_running_task,
        cancel_task=cancel_task,
        allowed_user_ids=cfg.allowed_user_ids,
        trigger_mode_default=cfg.trigger_mode_default,
        runtime=cfg.runtime,
        files=cfg.files,
        voice_manager=voice_manager,
    )

    # Register dynamic engine commands (/claude, /codex, etc.)
    engine_commands = register_engine_commands(
        cfg.bot,
        cfg=cfg,
        state_store=state_store,
        prefs_store=prefs_store,
        running_tasks=running_tasks,
        default_engine_override=default_engine_override,
    )
    if engine_commands:
        logger.info(
            "engine_commands.registered",
            count=len(engine_commands),
            commands=sorted(engine_commands),
        )

    # Discover and register plugin commands
    command_ids = discover_command_ids(cfg.runtime.allowlist)
    if command_ids:
        logger.info(
            "plugins.discovered",
            count=len(command_ids),
            ids=sorted(command_ids),
        )
        register_plugin_commands(
            cfg.bot,
            cfg,
            command_ids=command_ids,
            running_tasks=running_tasks,
            state_store=state_store,
            prefs_store=prefs_store,
            default_engine_override=default_engine_override,
        )
    else:
        logger.info("plugins.none_found")

    async def run_job(
        channel_id: int,
        user_msg_id: int,
        text: str,
        resume_token: ResumeToken | None,
        context: RunContext | None,
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

        logger.info(
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
            resolved = cfg.runtime.resolve_runner(
                resume_token=resume_token,
                engine_override=default_engine_override or engine_id,
            )
            if not resolved.available:
                logger.error(
                    "run_job.runner_unavailable",
                    engine=resolved.engine,
                    issue=resolved.issue,
                )
                return

            # Resolve working directory
            try:
                cwd = cfg.runtime.resolve_run_cwd(context)
            except ConfigError as exc:
                logger.error("run_job.cwd_error", error=str(exc))
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
                context_line = cfg.runtime.format_context_line(context)

                # Callback to save the resume token when it becomes known
                async def on_thread_known(
                    new_token: ResumeToken, done: anyio.Event
                ) -> None:
                    logger.debug(
                        "on_thread_known.called",
                        guild_id=guild_id,
                        channel_id=channel_id,
                        thread_id=thread_id,
                        token_preview=new_token.value[:20] + "..."
                        if len(new_token.value) > 20
                        else new_token.value,
                    )
                    # Save to thread_id if present, otherwise channel_id
                    # This matches the retrieval logic in handle_message
                    save_key = thread_id if thread_id else channel_id
                    await _save_session_token(
                        state_store=state_store,
                        guild_id=guild_id,
                        session_key=save_key,
                        author_id=author_id,
                        token=new_token,
                    )
                    if state_store and guild_id:
                        logger.info(
                            "session.saved",
                            guild_id=guild_id,
                            session_key=save_key,
                            author_id=author_id,
                            engine_id=new_token.engine,
                        )
                    else:
                        logger.debug(
                            "on_thread_known.not_saving",
                            has_state_store=state_store is not None,
                            guild_id=guild_id,
                        )
                    if scheduler is not None:
                        await scheduler.note_thread_known(new_token, done)

                with apply_run_options(run_options):
                    await tunapi_handle_message(
                        cfg.exec_cfg,
                        runner=resolved.runner,
                        incoming=incoming,
                        resume_token=resume_token,
                        context=context,
                        context_line=context_line,
                        strip_resume_line=cfg.runtime.is_resume_line,
                        running_tasks=running_tasks,
                        on_thread_known=on_thread_known,
                        progress_ref=progress_ref,
                    )
                logger.info("run_job.complete", channel_id=channel_id)
            finally:
                reset_run_base_dir(run_base_token)
        except Exception:
            logger.exception("run_job.error", channel_id=channel_id)
        finally:
            clear_context()

    async def run_thread_job(job: ThreadJob) -> None:
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
        if guild_id is not None:
            overrides = await resolve_overrides(
                prefs_store,
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

        await run_job(
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

    async def dispatch_media_group(state: _MediaGroupState) -> None:
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
            run_root = cfg.runtime.resolve_run_cwd(state.context)
        except ConfigError as exc:
            logger.warning("media_group.cwd_error", error=str(exc))
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
                    cfg.files.uploads_dir,
                    cfg.files.deny_globs,
                    max_bytes=cfg.files.max_upload_bytes,
                )
                if result.error is not None:
                    failures.append(f"`{attachment.filename}` ({result.error})")
                    continue
                if result.rel_path is None or result.size is None:
                    continue
                file_annotations.append(
                    f"[uploaded file: {result.rel_path.as_posix()}]"
                )
                saved_files.append(
                    f"`{result.rel_path.as_posix()}` ({format_bytes(result.size)})"
                )

        if not saved_files:
            return

        # No prompt text provided: confirm the upload(s) and return.
        if not prompt_text:
            parts = MarkdownParts(header="saved " + ", ".join(saved_files))
            text = prepare_discord(parts)
            reply_to = MessageRef(
                channel_id=state.job_channel_id,
                message_id=command_item.message.id,
                thread_id=state.thread_id,
            )
            await cfg.exec_cfg.transport.send(
                channel_id=state.job_channel_id,
                message=RenderedMessage(text=text, extra={"show_cancel": False}),
                options=SendOptions(
                    reply_to=reply_to, notify=False, thread_id=state.thread_id
                ),
            )
            return

        # Build prompt depending on auto_put_mode.
        combined_prompt = prompt_text
        if cfg.files.auto_put_mode == "prompt" and file_annotations:
            combined_prompt = "\n".join(file_annotations) + "\n\n" + prompt_text

        # Resolve model and reasoning overrides
        engine_id = state.engine_id or (cfg.runtime.default_engine or "claude")
        overrides = await resolve_overrides(
            prefs_store,
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

        # Include failures as a small preface if we can.
        if failures:
            combined_prompt = (
                "[upload failures]\n"
                + "\n".join(f"- {item}" for item in failures)
                + "\n\n"
                + combined_prompt
            )

        await run_job(
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

    async def handle_message(message: discord.Message) -> None:
        """Handle an incoming Discord message."""
        logger.debug(
            "message.raw",
            channel_type=type(message.channel).__name__,
            channel_id=message.channel.id,
            author=message.author.name,
            content_preview=message.content[:50] if message.content else "",
        )

        # Guild-only: ignore DMs
        if message.guild is None:
            logger.debug("message.skipped", reason="not in guild (DM)")
            return

        # Drain startup backlog: ignore messages sent before this bot instance started.
        if message.created_at < startup_cutoff:
            logger.debug(
                "message.skipped",
                reason="startup_backlog",
                created_at=str(message.created_at),
                startup_cutoff=str(startup_cutoff),
            )
            return

        if not should_process_message(message, cfg.bot.user, require_mention=False):
            logger.debug(
                "message.skipped", reason="should_process_message returned False"
            )
            return

        author_id = getattr(message.author, "id", None)
        if not isinstance(author_id, int):
            author_id = None
        if not is_user_allowed(cfg.allowed_user_ids, author_id):
            logger.debug(
                "message.skipped",
                reason="not in allowed_user_ids",
                author_id=author_id,
            )
            return
        files_allowed = is_user_allowed(cfg.files.allowed_user_ids, author_id)

        channel_id = message.channel.id
        guild_id = message.guild.id
        thread_id = None
        is_new_thread = False

        # Auto-set startup channel on first interaction (if not already set)
        if state_store and not isinstance(message.channel, discord.Thread):
            current_startup = await state_store.get_startup_channel(guild_id)
            if current_startup is None:
                await state_store.set_startup_channel(guild_id, channel_id)
                logger.info(
                    "startup_channel.auto_set",
                    guild_id=guild_id,
                    channel_id=channel_id,
                )

        # Check if this is a thread
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
            # Ensure we're a member of the thread so we receive future messages
            with contextlib.suppress(discord.HTTPException):
                await message.channel.join()

        # Get context from state
        # For threads, check thread-specific context first (set via @branch prefix)
        # Thread context has a specific branch; channel context uses worktree_base
        channel_context: DiscordChannelContext | None = None
        thread_context: DiscordThreadContext | None = None

        if state_store and guild_id:
            if thread_id:
                # Check if thread has its own bound context (from @branch prefix)
                ctx = await state_store.get_context(guild_id, thread_id)
                if isinstance(ctx, DiscordThreadContext):
                    thread_context = ctx

            # Always get channel context for project info and defaults
            ctx = await state_store.get_context(guild_id, channel_id)
            if isinstance(ctx, DiscordChannelContext):
                channel_context = ctx

        # Check trigger mode - may skip processing if mentions-only and not mentioned
        trigger_mode = await resolve_trigger_mode(
            prefs_store,
            guild_id,
            channel_id,
            thread_id,
            default_mode=cfg.trigger_mode_default,
        )
        if trigger_mode == "mentions":
            # Check if bot is mentioned or if this is a reply to the bot
            bot_mentioned = is_bot_mentioned(message, cfg.bot.user)
            is_reply_to_bot = False
            if message.reference and message.reference.message_id:
                # Check if replying to a bot message
                try:
                    ref_msg = await message.channel.fetch_message(
                        message.reference.message_id
                    )
                    is_reply_to_bot = ref_msg.author == cfg.bot.user
                except discord.NotFound:
                    pass
            if not bot_mentioned and not is_reply_to_bot:
                logger.debug(
                    "message.skipped",
                    reason="trigger_mode=mentions, bot not mentioned or replied to",
                )
                return

        # Determine effective context: thread context takes priority, otherwise use channel's worktree_base
        run_context: RunContext | None = None
        if thread_context:
            from tunapi.context import RunContext

            run_context = RunContext(
                project=thread_context.project,
                branch=thread_context.branch,
            )
        elif channel_context:
            from tunapi.context import RunContext

            # Use worktree_base as the default branch when no @branch specified
            run_context = RunContext(
                project=channel_context.project,
                branch=channel_context.worktree_base,
            )

        # Extract prompt
        prompt = extract_prompt_from_message(message, cfg.bot.user)

        # Parse @branch prefix (only for new messages in channels, not in existing threads)
        branch_override: str | None = None
        if thread_id is None:
            branch_override, prompt = parse_branch_prefix(prompt)
            if branch_override:
                logger.info("branch.override", branch=branch_override)

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

        if audio_attachments and not prompt.strip() and voice_attachment_transcriber:
            attachment = audio_attachments[0]
            if attachment.size > cfg.voice_messages.max_bytes:
                await message.reply(
                    "Voice message is too large to transcribe.\n"
                    f"- Size: {attachment.size} bytes\n"
                    f"- Max: {cfg.voice_messages.max_bytes} bytes",
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

            transcript = await voice_attachment_transcriber.transcribe_bytes(
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

        # Allow empty prompt if @branch was used or if there are attachments (for auto_put)
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
            and not cfg.voice_messages.enabled
        ):
            logger.debug("voice_messages.ignored", reason="disabled")
            return

        # Apply branch override to context
        if branch_override:
            from tunapi.context import RunContext

            if channel_context:
                # Override branch but keep project from channel
                run_context = RunContext(
                    project=channel_context.project,
                    branch=branch_override,
                )
            else:
                # No project bound - require /bind first
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
            cfg.session_mode == "chat" and branch_override is None
        )

        # Create thread for the response if not already in a thread.
        # In chat mode, plain channel conversations must stay in the same channel
        # so the same resume token is reused until the user explicitly starts a
        # separate thread or resets the session.
        if (
            auto_thread_enabled
            and thread_id is None
            and isinstance(message.channel, discord.TextChannel)
        ):
            # Thread name is just the branch if @branch was used, otherwise prompt snippet
            if branch_override:
                thread_name = branch_override
            else:
                thread_name = (
                    prompt[:100] if len(prompt) <= 100 else prompt[:97] + "..."
                )
            created_thread_id = await cfg.bot.create_thread(
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

                # Save thread context if @branch was used
                if branch_override and state_store and guild_id and channel_context:
                    new_thread_context = DiscordThreadContext(
                        project=channel_context.project,
                        branch=branch_override,
                        worktrees_dir=channel_context.worktrees_dir,
                        default_engine=channel_context.default_engine,
                    )
                    await state_store.set_context(
                        guild_id, thread_id, new_thread_context
                    )
                    logger.info(
                        "thread.context_saved",
                        thread_id=thread_id,
                        project=channel_context.project,
                        branch=branch_override,
                    )

                    # If @branch was used without a prompt, send confirmation and return
                    if not prompt.strip():
                        thread_channel = cfg.bot.bot.get_channel(thread_id)
                        if thread_channel and isinstance(
                            thread_channel, discord.Thread
                        ):
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

        # Get resume token to maintain conversation continuity
        # For threads, use thread_id as the session key to maintain conversation continuity
        # within the thread (regardless of which specific message is being replied to)
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
            has_state_store=state_store is not None,
        )

        # Resolve engine via preferences + bound context defaults.
        engine_id, engine_source = await resolve_effective_default_engine(
            prefs_store,
            guild_id=guild_id,
            channel_id=channel_id,
            thread_id=thread_id,
            bound_thread_default=thread_context.default_engine
            if thread_context
            else None,
            bound_channel_default=channel_context.default_engine
            if channel_context
            else None,
            config_default=cfg.runtime.default_engine,
        )
        if engine_id is None:
            engine_id = cfg.runtime.default_engine or "claude"
        logger.debug(
            "engine.resolved",
            engine_id=engine_id,
            source=engine_source,
            channel_id=channel_id,
            thread_id=thread_id,
        )

        reply_text: str | None = None

        # If the user is replying to one of our messages, prefer the engine from that
        # message header so reply chains continue the correct session/engine.
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
                and cfg.bot.user is not None
                and ref_msg.author == cfg.bot.user
            ):
                reply_text = _strip_ctx_lines(ref_msg.content)
                inferred = _extract_engine_id_from_header(ref_msg.content)
                if inferred and inferred in cfg.runtime.engine_ids:
                    engine_id = inferred
                    logger.debug(
                        "engine.inferred_from_reply",
                        engine_id=engine_id,
                        ref_message_id=ref_msg.id,
                    )

        # Prefer an explicit resume token (from message text or replied-to bot message)
        # over any stored "latest token".
        try:
            resolved_msg = cfg.runtime.resolve_message(
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

        if (
            resume_token is None
            and state_store
            and guild_id
            and cfg.session_mode == "chat"
        ):
            token_str = await state_store.get_session(
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

        # For new threads, don't set reply_ref since the original message is in the parent channel
        # and runner_bridge creates its own user_ref that would be incorrect for cross-channel replies
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

        # Buffer attachment-only bursts in existing threads to handle "media groups"
        # (multiple file messages sent together).
        if (
            media_buffer is not None
            and isinstance(message.channel, discord.Thread)
            and thread_id is not None
            and author_id is not None
            and cfg.files.enabled
            and cfg.files.auto_put
            and files_allowed
            and run_context is not None
            and run_context.project is not None
        ):
            if attachments_for_files and not prompt.strip():
                media_buffer.add(
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
                and media_buffer.has_pending(channel_id=thread_id, author_id=author_id)
            ):
                media_buffer.add(
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

        # Handle auto_put for file attachments
        logger.debug(
            "auto_put.check",
            files_enabled=cfg.files.enabled,
            auto_put=cfg.files.auto_put,
            attachment_count=len(attachments_for_files),
        )
        if (
            cfg.files.enabled
            and cfg.files.auto_put
            and attachments_for_files
            and files_allowed
        ):
            from tunapi.config import ConfigError

            from .file_transfer import format_bytes, save_attachment

            # Need a project context to save files
            if run_context is None or run_context.project is None:
                logger.debug(
                    "auto_put.skipped",
                    reason="no project context",
                    attachment_count=len(attachments_for_files),
                )
            else:
                try:
                    run_root = cfg.runtime.resolve_run_cwd(run_context)
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
                            cfg.files.uploads_dir,
                            cfg.files.deny_globs,
                            max_bytes=cfg.files.max_upload_bytes,
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

                    # Handle based on auto_put_mode
                    if cfg.files.auto_put_mode == "prompt" and file_annotations:
                        # Prepend file annotations to the prompt
                        prompt = "\n".join(file_annotations) + "\n\n" + prompt
                        logger.debug(
                            "auto_put.annotated",
                            annotation_count=len(file_annotations),
                        )
                    elif cfg.files.auto_put_mode == "upload" and saved_files:
                        # Just confirm the upload if no prompt
                        if not prompt.strip():
                            confirm_msg = "saved " + ", ".join(saved_files)
                            await message.reply(confirm_msg, mention_author=False)
                            return

        # For new threads, use thread_id as channel_id since that's where we're sending
        # For existing threads/channels, thread_id already specifies where to send
        job_channel_id = thread_id if thread_id else channel_id

        # Replies to a running task's progress message should be queued until the
        # current task's resume token is ready, then executed with full context.
        # Also queue any message that resumes an existing thread to avoid
        # overlapping runs for the same conversation.
        session_meta: tuple[int, int | None] | tuple[int, int | None, int] = (
            (guild_id, channel_id, author_id)
            if author_id is not None
            else (guild_id, channel_id)
        )
        if resume_resolver is not None:
            reply_id = (
                message.reference.message_id
                if message.reference and message.reference.message_id
                else None
            )
            decision = await resume_resolver.resolve(
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

        if resume_token is not None and scheduler is not None:
            progress_ref = await _send_queued_progress(
                cfg,
                channel_id=job_channel_id,
                user_msg_id=message.id,
                thread_id=thread_id,
                resume_token=resume_token,
                context=run_context,
            )
            await scheduler.enqueue_resume(
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

        # Resolve model and reasoning overrides
        overrides = await resolve_overrides(
            prefs_store, guild_id, channel_id, thread_id, engine_id
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
            await run_job(
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

    # Set up message handler
    cfg.bot.set_message_handler(handle_message)

    # Handle cancel button interactions
    @cfg.bot.bot.event
    async def on_interaction(interaction: discord.Interaction) -> None:
        # Handle component interactions (buttons)
        if interaction.type == discord.InteractionType.component:
            if interaction.data:
                custom_id = interaction.data.get("custom_id")
                if custom_id == CANCEL_BUTTON_ID:
                    user_id = getattr(getattr(interaction, "user", None), "id", None)
                    if not isinstance(user_id, int):
                        user_id = None
                    if not is_user_allowed(cfg.allowed_user_ids, user_id):
                        await interaction.response.defer()
                        return
                    # Get the channel where the cancel was clicked
                    channel_id = interaction.channel_id
                    if channel_id is not None:
                        await cancel_task(channel_id)
                    await interaction.response.defer()
            return

        # For application commands, let Pycord handle them
        # This is required when overriding on_interaction
        await cfg.bot.bot.process_application_commands(interaction)

    # Auto-join new threads so we receive messages from them
    @cfg.bot.bot.event
    async def on_thread_create(thread: discord.Thread) -> None:
        with contextlib.suppress(discord.HTTPException):
            await thread.join()
            logger.debug("thread.auto_joined", thread_id=thread.id, name=thread.name)

    # Handle voice state updates (users joining/leaving voice channels)
    if voice_manager is not None:

        @cfg.bot.bot.event
        async def on_voice_state_update(
            member: discord.Member,
            before: discord.VoiceState,
            after: discord.VoiceState,
        ) -> None:
            await voice_manager.handle_voice_state_update(member, before, after)

        # Set up voice message handler
        async def handle_voice_message(
            guild_id: int,
            text_channel_id: int,
            transcript: str,
            user_name: str,
            project: str,
            branch: str,
        ) -> str | None:
            """Handle a transcribed voice message.

            Routes through Claude/tunapi for full conversation context.
            Says "Working on it" immediately, then TTS the final response.
            """
            from tunapi.context import RunContext

            logger.info(
                "voice.message",
                guild_id=guild_id,
                text_channel_id=text_channel_id,
                user_name=user_name,
                transcript_length=len(transcript),
            )

            # Post the transcribed message to the text channel
            transport = cast(DiscordTransport, cfg.exec_cfg.transport)
            await transport.send(
                channel_id=text_channel_id,
                message=RenderedMessage(
                    text=f"🎤 **{user_name}**: {transcript}",
                    extra={},
                ),
            )

            # Say "Working on it" via TTS immediately
            # Return this first, then process through Claude
            # The final response will be captured via message listener

            # Set up a listener to capture the final response for TTS
            final_response: list[str] = []
            response_event = anyio.Event()

            async def on_message(channel_id: int, text: str, is_final: bool) -> None:
                if is_final and text:
                    # Extract just the answer text from the formatted message
                    # The format is typically: header + answer + footer
                    # We want just the main content for TTS
                    final_response.append(text)
                    response_event.set()

            # Register the listener
            transport.add_message_listener(text_channel_id, on_message)

            try:
                # Build run context
                run_context = RunContext(project=project, branch=branch)

                # Get resume token for the text channel
                resume_token: ResumeToken | None = None
                engine_id = cfg.runtime.default_engine or "claude"
                if cfg.session_mode == "chat":
                    token_str = await state_store.get_session(
                        guild_id, text_channel_id, engine_id
                    )
                    if token_str:
                        resume_token = ResumeToken(engine=engine_id, value=token_str)

                # Use run_job to process the voice message through Claude
                import time

                voice_msg_id = int(time.time() * 1000)

                # Run the job (this will send progress updates and final response)
                await run_job(
                    channel_id=text_channel_id,
                    user_msg_id=voice_msg_id,
                    text=transcript,
                    resume_token=resume_token,
                    context=run_context,
                    engine_id=engine_id,
                    thread_id=None,
                    reply_ref=None,
                    guild_id=guild_id,
                )

                # Wait briefly for the final response to be captured
                with anyio.move_on_after(5.0):
                    await response_event.wait()

                if final_response:
                    # Extract a TTS-friendly summary from the response
                    response_text = final_response[0]

                    # Strip markdown formatting for cleaner TTS
                    import re

                    # Remove the first line (status line like "✅ done · claude · 10s")
                    lines = response_text.split("\n")
                    response_text = "\n".join(lines[1:]) if len(lines) > 1 else ""
                    # Remove code blocks
                    response_text = re.sub(r"```[\s\S]*?```", "", response_text)
                    # Remove inline code
                    response_text = re.sub(r"`[^`]+`", "", response_text)
                    # Remove bold/italic markers
                    response_text = re.sub(r"\*+([^*]+)\*+", r"\1", response_text)
                    # Remove headers
                    response_text = re.sub(
                        r"^#+\s+", "", response_text, flags=re.MULTILINE
                    )
                    # Remove resume lines (e.g., "↩️ resume: ...")
                    response_text = re.sub(
                        r"^↩️.*$", "", response_text, flags=re.MULTILINE
                    )
                    # Clean up whitespace
                    response_text = re.sub(r"\n{3,}", "\n\n", response_text).strip()

                    # Truncate for TTS if too long (keep first ~500 chars)
                    if len(response_text) > 500:
                        response_text = response_text[:500] + "..."

                    # Skip if nothing meaningful left after stripping
                    if not response_text or len(response_text) < 5:
                        return None

                    logger.info(
                        "voice.response",
                        guild_id=guild_id,
                        response_length=len(response_text),
                    )

                    return response_text

            except Exception:
                logger.exception("voice.response_error")

            finally:
                # Clean up the listener
                transport.remove_message_listener(text_channel_id)

            return None

        voice_manager.set_message_handler(handle_voice_message)

    # Config file watching state
    transport_snapshot: dict[str, Any] | None = (
        dict(transport_config) if transport_config is not None else None
    )
    current_command_ids: set[str] = command_ids.copy() if command_ids else set()

    def refresh_commands() -> set[str]:
        """Refresh the set of discovered command IDs."""
        nonlocal current_command_ids
        new_ids = discover_command_ids(cfg.runtime.allowlist)
        current_command_ids = new_ids
        return new_ids

    async def handle_reload(reload: ConfigReload) -> None:
        """Handle config file reload."""
        nonlocal transport_snapshot

        # Refresh command IDs
        old_command_ids = current_command_ids.copy()
        new_command_ids = refresh_commands()

        # Check for new commands that need registration
        added_commands = new_command_ids - old_command_ids
        removed_commands = old_command_ids - new_command_ids

        if added_commands or removed_commands:
            logger.info(
                "config.reload.commands_changed",
                added=sorted(added_commands) if added_commands else None,
                removed=sorted(removed_commands) if removed_commands else None,
            )

            # Register new plugin commands
            if added_commands:
                register_plugin_commands(
                    cfg.bot,
                    cfg,
                    command_ids=added_commands,
                    running_tasks=running_tasks,
                    state_store=state_store,
                    prefs_store=prefs_store,
                    default_engine_override=default_engine_override,
                )

            # Sync commands with Discord
            # Note: removed commands won't be unregistered until bot restart
            # because Pycord doesn't support dynamic command removal
            try:
                await cfg.bot.bot.sync_commands()
                logger.info("config.reload.commands_synced")
            except discord.HTTPException as exc:
                logger.warning(
                    "config.reload.sync_failed",
                    error=str(exc),
                    error_type=exc.__class__.__name__,
                )

            if removed_commands:
                logger.warning(
                    "config.reload.commands_removed",
                    commands=sorted(removed_commands),
                    restart_required=True,
                )

        # Check for transport config changes
        if transport_snapshot is not None:
            # Discord config is in model_extra since it's a plugin transport
            new_snapshot = getattr(reload.settings.transports, "model_extra", {}).get(
                "discord"
            )
            if isinstance(new_snapshot, dict):
                changed = _diff_keys(transport_snapshot, new_snapshot)
                if changed:
                    logger.warning(
                        "config.reload.transport_config_changed",
                        transport="discord",
                        keys=changed,
                        restart_required=True,
                    )
                    transport_snapshot = new_snapshot

    watch_enabled = config_path is not None

    async def run_with_watcher() -> None:
        """Run the main loop with optional config watcher."""
        nonlocal scheduler, resume_resolver, media_buffer
        async with anyio.create_task_group() as tg:
            scheduler = ThreadScheduler(task_group=tg, run_job=run_thread_job)
            resume_resolver = ResumeResolver(
                cfg=cfg,
                task_group=tg,
                running_tasks=running_tasks,
                enqueue_resume=scheduler.enqueue_resume,
            )
            if cfg.media_group_debounce_s > 0:
                media_buffer = MediaGroupBuffer(
                    task_group=tg,
                    debounce_s=cfg.media_group_debounce_s,
                    dispatch=dispatch_media_group,
                )
            else:
                media_buffer = None

            # Start the bot
            await cfg.bot.start()

            # Send startup message to configured channel or first available text channel
            if cfg.guild_id:
                startup_channel_id = await state_store.get_startup_channel(cfg.guild_id)
                if startup_channel_id:
                    await _send_startup(cfg, startup_channel_id)
                    logger.info(
                        "startup.configured_channel", channel_id=startup_channel_id
                    )
                else:
                    guild = cfg.bot.get_guild(cfg.guild_id)
                    if guild:
                        for channel in guild.text_channels:
                            await _send_startup(cfg, channel.id)
                            logger.info(
                                "startup.first_channel",
                                channel_id=channel.id,
                                hint="mention bot in preferred channel to set as startup channel",
                            )
                            break

            logger.info(
                "bot.ready", user=cfg.bot.user.name if cfg.bot.user else "unknown"
            )

            if watch_enabled and config_path is not None:

                async def run_config_watch() -> None:
                    await watch_config_changes(
                        config_path=config_path,
                        runtime=cfg.runtime,
                        default_engine_override=default_engine_override,
                        on_reload=handle_reload,
                    )

                tg.start_soon(run_config_watch)
                logger.info("config.watch.started", path=str(config_path))

            # Keep running until cancelled
            await anyio.sleep_forever()

    try:
        await run_with_watcher()
    finally:
        await cfg.bot.close()
