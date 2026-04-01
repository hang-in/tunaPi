"""Discord API client wrapper."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio
import discord

from tunapi.logging import get_logger

from .outbox import (
    DELETE_PRIORITY,
    EDIT_PRIORITY,
    SEND_PRIORITY,
    DiscordOutbox,
    OutboxOp,
    RetryAfter,
)

logger = get_logger(__name__)

# Default rate limit: ~1 message per second per channel (Discord: 5/5s)
DEFAULT_CHANNEL_RPS = 1.0

if TYPE_CHECKING:
    from collections.abc import Coroutine

    MessageHandler = Callable[[discord.Message], Coroutine[Any, Any, None]]
    InteractionHandler = Callable[[discord.Interaction], Coroutine[Any, Any, None]]


@dataclass(frozen=True, slots=True)
class SentMessage:
    """Result of sending a message."""

    message_id: int
    channel_id: int
    thread_id: int | None = None


class DiscordBotClient:
    """Wrapper around Pycord Bot for tunapi integration."""

    def __init__(
        self,
        token: str,
        *,
        guild_id: int | None = None,
        channel_rps: float = DEFAULT_CHANNEL_RPS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self._token = token
        self._guild_id = guild_id
        self._message_handler: MessageHandler | None = None
        self._interaction_handler: InteractionHandler | None = None
        # Defer bot creation until inside async context (Python 3.10+ compatibility)
        self._bot: discord.Bot | None = None
        self._ready_event: asyncio.Event | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._startup_error: Exception | None = None
        # Rate limiting
        self._clock = clock
        self._sleep = sleep
        self._channel_interval = 0.0 if channel_rps <= 0 else 1.0 / channel_rps
        self._outbox = DiscordOutbox(
            interval_for_channel=self.interval_for_channel,
            clock=clock,
            sleep=sleep,
            on_error=self._log_request_error,
            on_outbox_error=self._log_outbox_failure,
        )
        self._seq = itertools.count()

    def _ensure_bot(self) -> discord.Bot:
        """Create the bot if not already created. Must be called from async context."""
        if self._bot is not None:
            return self._bot

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True  # Required for voice channel member tracking
        # Required for receiving messages in threads
        intents.messages = True
        # Required for voice channel functionality
        intents.voice_states = True
        # Use discord.Bot which has built-in slash command support
        # debug_guilds ensures instant command sync to specific guilds
        debug_guilds = [self._guild_id] if self._guild_id else None
        self._bot = discord.Bot(intents=intents, debug_guilds=debug_guilds)
        self._ready_event = asyncio.Event()
        self._startup_error = None

        @self._bot.event
        async def on_ready() -> None:
            assert self._ready_event is not None
            logger.info(
                "gateway.connected",
                user=self._bot.user.name if self._bot.user is not None else None,
                user_id=self._bot.user.id if self._bot.user is not None else None,
                guild_id=self._guild_id,
            )
            self._ready_event.set()

        @self._bot.event
        async def on_message(message: discord.Message) -> None:
            assert self._bot is not None
            if message.author == self._bot.user:
                return
            if self._message_handler is not None:
                await self._message_handler(message)

        return self._bot

    @property
    def bot(self) -> discord.Bot:
        """Get the underlying Pycord bot. Creates it if needed."""
        return self._ensure_bot()

    @property
    def user(self) -> discord.User | None:
        """Get the bot user."""
        if self._bot is None:
            return None
        return self._bot.user

    def set_message_handler(self, handler: MessageHandler) -> None:
        """Set the message handler."""
        self._message_handler = handler

    def set_interaction_handler(self, handler: InteractionHandler) -> None:
        """Set the interaction handler for non-command interactions."""
        self._interaction_handler = handler

    def interval_for_channel(self, channel_id: int | None) -> float:
        """Get the rate limit interval for a channel."""
        return self._channel_interval

    def _log_request_error(self, request: OutboxOp, exc: Exception) -> None:
        """Log an error from an individual request."""
        logger.error(
            "discord.outbox.request_failed",
            method=request.label,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )

    def _log_outbox_failure(self, exc: Exception) -> None:
        """Log a fatal outbox error."""
        logger.error(
            "discord.outbox.failed",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )

    def _unique_key(self, prefix: str) -> tuple[str, int]:
        """Generate a unique key for non-coalescing operations."""
        return (prefix, next(self._seq))

    async def _enqueue_op(
        self,
        *,
        key: Hashable,
        label: str,
        execute: Callable[[], Awaitable[Any]],
        priority: int,
        channel_id: int | None,
        wait: bool = True,
    ) -> Any:
        """Enqueue an operation in the outbox."""
        request = OutboxOp(
            execute=execute,
            priority=priority,
            queued_at=self._clock(),
            channel_id=channel_id,
            label=label,
        )
        return await self._outbox.enqueue(key=key, op=request, wait=wait)

    async def drop_pending_edits(self, *, channel_id: int, message_id: int) -> None:
        """Drop pending edit operations for a message."""
        await self._outbox.drop_pending(key=("edit", channel_id, message_id))

    def _extract_retry_after(self, exc: discord.HTTPException) -> float:
        """Extract retry_after value from a Discord HTTPException."""
        # Try to get from response headers
        if hasattr(exc, "response") and exc.response is not None:
            retry_after = exc.response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    return float(retry_after)
                except ValueError:
                    pass
        # Try to get from JSON body (if available in exc.text)
        if hasattr(exc, "text") and exc.text:
            import json

            try:
                data = json.loads(exc.text)
                if isinstance(data, dict) and "retry_after" in data:
                    return float(data["retry_after"])
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        # Default to 1 second if we can't extract the value
        return 1.0

    async def start(self) -> None:
        """Start the bot and wait until ready."""
        bot = self._ensure_bot()
        assert self._ready_event is not None
        self._startup_error = None
        logger.info("gateway.connecting", guild_id=self._guild_id)

        async def _run_bot() -> None:
            try:
                await bot.start(self._token)
            except asyncio.CancelledError:
                pass
            except RuntimeError as e:
                # Suppress "Session is closed" error during shutdown
                if "Session is closed" not in str(e):
                    self._startup_error = e
                    self._ready_event.set()
                    logger.exception(
                        "gateway.start_failed",
                        error=str(e),
                        error_type=e.__class__.__name__,
                    )
                    raise
            except Exception as exc:  # noqa: BLE001
                self._startup_error = exc
                self._ready_event.set()
                logger.exception(
                    "gateway.start_failed",
                    error=str(exc),
                    error_type=exc.__class__.__name__,
                )
                raise

        self._start_task = asyncio.create_task(_run_bot(), name="discord-bot-start")
        await self._ready_event.wait()
        if self._startup_error is not None:
            raise self._startup_error

    async def close(self) -> None:
        """Close the bot connection."""
        await self._outbox.close()
        if self._bot is not None:
            await self._bot.close()
            # Cancel the start task and wait for it to finish
            if self._start_task is not None and not self._start_task.done():
                self._start_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._start_task

    async def wait_until_ready(self) -> None:
        """Wait until the bot is ready."""
        self._ensure_bot()
        assert self._ready_event is not None
        await self._ready_event.wait()

    async def send_message(
        self,
        *,
        channel_id: int,
        content: str,
        reply_to_message_id: int | None = None,
        thread_id: int | None = None,
        view: discord.ui.View | None = None,
        embed: discord.Embed | None = None,
        suppress_embeds: bool = True,
    ) -> SentMessage | None:
        """Send a message to a channel (queued with rate limiting)."""

        async def execute() -> SentMessage | None:
            return await self._send_message_impl(
                channel_id=channel_id,
                content=content,
                reply_to_message_id=reply_to_message_id,
                thread_id=thread_id,
                view=view,
                embed=embed,
                suppress_embeds=suppress_embeds,
            )

        return await self._enqueue_op(
            key=self._unique_key("send"),
            label="send_message",
            execute=execute,
            priority=SEND_PRIORITY,
            channel_id=thread_id or channel_id,
        )

    async def _send_message_impl(
        self,
        *,
        channel_id: int,
        content: str,
        reply_to_message_id: int | None = None,
        thread_id: int | None = None,
        view: discord.ui.View | None = None,
        embed: discord.Embed | None = None,
        suppress_embeds: bool = True,
    ) -> SentMessage | None:
        """Internal implementation of send_message."""
        channel = self._bot.get_channel(thread_id or channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(thread_id or channel_id)
            except discord.NotFound:
                return None

        if not isinstance(channel, discord.abc.Messageable):
            return None

        reference = None
        if reply_to_message_id is not None:
            # Use thread_id for the reference channel if we're in a thread,
            # since that's where the original message actually exists
            reference = discord.MessageReference(
                message_id=reply_to_message_id,
                channel_id=thread_id or channel_id,
            )

        try:
            kwargs: dict[str, Any] = {"content": content, "suppress": suppress_embeds}
            if reference is not None:
                kwargs["reference"] = reference
            if view is not None:
                kwargs["view"] = view
            if embed is not None:
                kwargs["embed"] = embed

            message = await channel.send(**kwargs)
            return SentMessage(
                message_id=message.id,
                channel_id=message.channel.id,
                thread_id=thread_id,
            )
        except discord.HTTPException as exc:
            # Check for rate limit
            if exc.status == 429:
                retry_after = self._extract_retry_after(exc)
                raise RetryAfter(retry_after, "Discord rate limit") from exc
            # If send failed and we had a reference, retry without it
            # This handles cases like new threads where the reply message
            # might not be in the thread
            if reference is not None:
                try:
                    kwargs.pop("reference", None)
                    message = await channel.send(**kwargs)
                    return SentMessage(
                        message_id=message.id,
                        channel_id=message.channel.id,
                        thread_id=thread_id,
                    )
                except discord.HTTPException as retry_exc:
                    if retry_exc.status == 429:
                        retry_after = self._extract_retry_after(retry_exc)
                        raise RetryAfter(
                            retry_after, "Discord rate limit"
                        ) from retry_exc
                    return None
            return None

    async def edit_message(
        self,
        *,
        channel_id: int,
        message_id: int,
        content: str,
        view: discord.ui.View | None = None,
        embed: discord.Embed | None = None,
        suppress_embeds: bool = True,
        wait: bool = True,
    ) -> SentMessage | None:
        """Edit an existing message (queued with rate limiting and coalescing)."""

        async def execute() -> SentMessage | None:
            return await self._edit_message_impl(
                channel_id=channel_id,
                message_id=message_id,
                content=content,
                view=view,
                embed=embed,
                suppress_embeds=suppress_embeds,
            )

        return await self._enqueue_op(
            key=("edit", channel_id, message_id),
            label="edit_message",
            execute=execute,
            priority=EDIT_PRIORITY,
            channel_id=channel_id,
            wait=wait,
        )

    async def _edit_message_impl(
        self,
        *,
        channel_id: int,
        message_id: int,
        content: str,
        view: discord.ui.View | None = None,
        embed: discord.Embed | None = None,
        suppress_embeds: bool = True,
    ) -> SentMessage | None:
        """Internal implementation of edit_message."""
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(channel_id)
            except discord.NotFound:
                return None

        if not isinstance(channel, discord.abc.Messageable):
            return None

        try:
            message = await channel.fetch_message(message_id)
            kwargs: dict[str, Any] = {"content": content, "suppress": suppress_embeds}
            if view is not None:
                kwargs["view"] = view
            if embed is not None:
                kwargs["embed"] = embed

            edited = await message.edit(**kwargs)
            return SentMessage(
                message_id=edited.id,
                channel_id=edited.channel.id,
            )
        except discord.HTTPException as exc:
            if exc.status == 429:
                retry_after = self._extract_retry_after(exc)
                raise RetryAfter(retry_after, "Discord rate limit") from exc
            return None

    async def delete_message(
        self,
        *,
        channel_id: int,
        message_id: int,
    ) -> bool:
        """Delete a message (queued with rate limiting)."""
        # Drop any pending edits for this message before deleting
        await self.drop_pending_edits(channel_id=channel_id, message_id=message_id)

        async def execute() -> bool:
            return await self._delete_message_impl(
                channel_id=channel_id,
                message_id=message_id,
            )

        result = await self._enqueue_op(
            key=("delete", channel_id, message_id),
            label="delete_message",
            execute=execute,
            priority=DELETE_PRIORITY,
            channel_id=channel_id,
        )
        return bool(result)

    async def _delete_message_impl(
        self,
        *,
        channel_id: int,
        message_id: int,
    ) -> bool:
        """Internal implementation of delete_message."""
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(channel_id)
            except discord.NotFound:
                return False

        if not isinstance(channel, discord.abc.Messageable):
            return False

        try:
            message = await channel.fetch_message(message_id)
            await message.delete()
            return True
        except discord.HTTPException as exc:
            if exc.status == 429:
                retry_after = self._extract_retry_after(exc)
                raise RetryAfter(retry_after, "Discord rate limit") from exc
            return False

    async def create_thread(
        self,
        *,
        channel_id: int,
        message_id: int,
        name: str,
        auto_archive_duration: int = 1440,  # 24 hours
    ) -> int | None:
        """Create a thread from a message."""
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(channel_id)
            except discord.NotFound:
                return None

        if not isinstance(channel, discord.TextChannel):
            return None

        try:
            message = await channel.fetch_message(message_id)
            thread = await message.create_thread(
                name=name,
                auto_archive_duration=auto_archive_duration,
            )
            # Join the thread so we receive messages from it
            await thread.join()
            return thread.id
        except discord.HTTPException:
            return None

    async def create_thread_without_message(
        self,
        *,
        channel_id: int,
        name: str,
        auto_archive_duration: int = 1440,  # 24 hours
    ) -> int | None:
        """Create a thread without a starter message."""
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(channel_id)
            except discord.NotFound:
                return None

        if not isinstance(channel, discord.TextChannel):
            return None

        try:
            thread = await channel.create_thread(
                name=name,
                message=None,
                auto_archive_duration=auto_archive_duration,
                type=discord.ChannelType.public_thread,
            )
            await thread.join()
            return thread.id
        except discord.HTTPException:
            return None

    def get_guild(self, guild_id: int) -> discord.Guild | None:
        """Get a guild by ID."""
        return self._bot.get_guild(guild_id)

    def get_channel(self, channel_id: int) -> discord.abc.GuildChannel | None:
        """Get a channel by ID."""
        channel = self._bot.get_channel(channel_id)
        if isinstance(channel, discord.abc.GuildChannel):
            return channel
        return None
