"""Main event loop for Discord transport."""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import discord

from tunapi.config_watch import ConfigReload, watch_config as watch_config_changes
from tunapi.core.roundtable import (
    RoundtableStore,
)
from tunapi.logging import get_logger
from tunapi.model import ResumeToken
from tunapi.runner_bridge import RunningTasks
from tunapi.scheduler import ThreadScheduler
from tunapi.transport import RenderedMessage

from .allowlist import is_user_allowed
from .loop_state import (
    DiscordLoopContext,
    MediaGroupBuffer,
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
    should_process_message,
    register_engine_commands,
    register_slash_commands,
)
from .overrides import (
    resolve_effective_default_engine,
    resolve_overrides,
    resolve_trigger_mode,
)
from .prefs import DiscordPrefsStore
from .state import DiscordStateStore
from .voice_messages import WhisperAttachmentTranscriber, is_audio_attachment


from tunapi.core.roundtable import (
    run_followup_round,
    run_roundtable,
)

from .resume_dispatch import (
    ResumeResolver,
    _save_session_token,
    _send_plain_reply,
    _send_queued_progress,
    _send_startup,
    _wait_for_resume,
    send_with_resume,
)
from .roundtable_dispatch import (
    _archive_roundtable,
    _dispatch_rt_command,
    _start_roundtable,
)
from .job_handlers import (
    dispatch_media_group,
    run_job,
    run_thread_job,
)
from .loop_dispatch import (
    handle_message,
)


if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

__all__ = [
    "run_main_loop",
    "ResumeResolver",
    "send_with_resume",
    "_save_session_token",
    "_send_plain_reply",
    "_send_queued_progress",
    "_send_startup",
    "_wait_for_resume",
    "_start_roundtable",
    "_archive_roundtable",
    "_dispatch_rt_command",
    "run_job",
    "run_thread_job",
    "dispatch_media_group",
    "handle_message",
    "is_user_allowed",
    "should_process_message",
    "extract_prompt_from_message",
    "resolve_trigger_mode",
    "parse_branch_prefix",
    "resolve_effective_default_engine",
    "resolve_overrides",
    "run_roundtable",
    "run_followup_round",
    "_extract_engine_id_from_header",
    "_strip_ctx_lines",
    "is_audio_attachment",
    "is_bot_mentioned",
]


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
    roundtable_store = RoundtableStore(
        cfg.runtime.config_path / "discord_roundtables.json"
    )
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

    def get_context() -> DiscordLoopContext:
        """Create a DiscordLoopContext on demand with current active states."""
        return DiscordLoopContext(
            cfg=cfg,
            running_tasks=running_tasks,
            state_store=state_store,
            prefs_store=prefs_store,
            roundtable_store=roundtable_store,
            scheduler=scheduler,
            resume_resolver=resume_resolver,
            media_buffer=media_buffer,
            voice_manager=voice_manager,
            voice_attachment_transcriber=voice_attachment_transcriber,
            default_engine_override=default_engine_override,
            startup_cutoff=startup_cutoff,
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

    # Register roundtable slash command (/rt)
    from .handlers import register_roundtable_command

    register_roundtable_command(
        cfg.bot,
        cfg=cfg,
        running_tasks=running_tasks,
        roundtable_store=roundtable_store,
        state_store=state_store,
        allowed_user_ids=cfg.allowed_user_ids,
    )
    logger.info("roundtable.command_registered")

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
                    get_context(),
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
            scheduler = ThreadScheduler(
                task_group=tg,
                run_job=lambda job: run_thread_job(get_context(), job),
            )
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
                    dispatch=lambda state: dispatch_media_group(get_context(), state),
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

    # Set up message handler
    async def handle_message_wrapped(message: discord.Message) -> None:
        await handle_message(get_context(), message)

    cfg.bot.set_message_handler(handle_message_wrapped)

    try:
        await run_with_watcher()
    finally:
        await cfg.bot.close()
