"""Discord transport and presenter implementation."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

import discord

from tunapi.markdown import MarkdownFormatter
from tunapi.progress import ProgressState
from tunapi.transport import MessageRef, RenderedMessage, SendOptions

from .client import DiscordBotClient
from .render import MAX_BODY_CHARS, prepare_discord, prepare_discord_multi

if TYPE_CHECKING:
    from tunapi.runner_bridge import ExecBridgeConfig
    from tunapi.transport_runtime import TransportRuntime

__all__ = [
    "DiscordBridgeConfig",
    "DiscordFilesSettings",
    "DiscordPresenter",
    "DiscordTransport",
    "DiscordVoiceMessageSettings",
]

CANCEL_BUTTON_ID = "tunapi-discord:cancel"


class CancelView(discord.ui.View):
    """View with cancel button."""

    def __init__(self, *, on_cancel: callable | None = None) -> None:
        super().__init__(timeout=None)
        self._on_cancel = on_cancel

    @discord.ui.button(
        label="cancel", style=discord.ButtonStyle.secondary, custom_id=CANCEL_BUTTON_ID
    )
    async def cancel_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        if self._on_cancel is not None:
            await self._on_cancel(interaction)
        else:
            await interaction.response.defer()


class ClearView(discord.ui.View):
    """Empty view to clear buttons."""

    def __init__(self) -> None:
        super().__init__(timeout=None)


class DiscordPresenter:
    """Presenter for rendering messages to Discord format."""

    def __init__(
        self,
        *,
        formatter: MarkdownFormatter | None = None,
        message_overflow: str = "split",
    ) -> None:
        self._formatter = formatter or MarkdownFormatter()
        self._message_overflow = message_overflow

    def render_progress(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        label: str = "working",
    ) -> RenderedMessage:
        """Render a progress update message."""
        parts = self._formatter.render_progress_parts(
            state, elapsed_s=elapsed_s, label=label
        )
        text = prepare_discord(parts)
        is_cancelled = _is_cancelled_label(label)
        return RenderedMessage(
            text=text,
            extra={
                "show_cancel": not is_cancelled,
            },
        )

    def render_final(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        status: str,
        answer: str,
    ) -> RenderedMessage:
        """Render a final response message."""
        parts = self._formatter.render_final_parts(
            state, elapsed_s=elapsed_s, status=status, answer=answer
        )
        if self._message_overflow == "split":
            messages = prepare_discord_multi(parts, max_body_chars=MAX_BODY_CHARS)
            text = messages[0]
            extra: dict = {"show_cancel": False}
            if len(messages) > 1:
                followups = [
                    RenderedMessage(text=msg, extra={"show_cancel": False})
                    for msg in messages[1:]
                ]
                extra["followups"] = followups
            return RenderedMessage(text=text, extra=extra)
        text = prepare_discord(parts)
        return RenderedMessage(text=text, extra={"show_cancel": False})


def _is_cancelled_label(label: str) -> bool:
    stripped = label.strip()
    if stripped.startswith("`") and stripped.endswith("`") and len(stripped) >= 2:
        stripped = stripped[1:-1]
    return stripped.lower() == "cancelled"


@dataclass(frozen=True, slots=True)
class DiscordFilesSettings:
    """Settings for file transfer functionality."""

    enabled: bool = False
    auto_put: bool = True
    auto_put_mode: Literal["upload", "prompt"] = "upload"
    uploads_dir: str = "incoming"
    max_upload_bytes: int = 20 * 1024 * 1024  # 20MB
    deny_globs: tuple[str, ...] = (
        ".git/**",
        ".env",
        ".envrc",
        "**/*.pem",
        "**/.ssh/**",
    )
    allowed_user_ids: frozenset[int] | None = None


@dataclass(frozen=True, slots=True)
class DiscordVoiceMessageSettings:
    """Settings for voice message attachment transcription in text chat."""

    enabled: bool = False
    max_bytes: int = 10 * 1024 * 1024  # 10MB
    whisper_model: str = "base"


@dataclass(frozen=True, slots=True)
class DiscordBridgeConfig:
    """Configuration for the Discord bridge."""

    bot: DiscordBotClient
    runtime: TransportRuntime
    guild_id: int | None
    startup_msg: str
    exec_cfg: ExecBridgeConfig
    allowed_user_ids: frozenset[int] | None = None
    session_mode: Literal["stateless", "chat"] = "stateless"
    show_resume_line: bool = True
    message_overflow: Literal["trim", "split"] = "split"
    trigger_mode_default: Literal["all", "mentions"] = "all"
    files: DiscordFilesSettings = DiscordFilesSettings()
    voice_messages: DiscordVoiceMessageSettings = DiscordVoiceMessageSettings()
    media_group_debounce_s: float = 0.75


# Type alias for message listener callbacks
MessageListener = (
    callable  # (channel_id: int, text: str, is_final: bool) -> Awaitable[None]
)


class DiscordTransport:
    """Transport implementation for Discord."""

    def __init__(self, bot: DiscordBotClient) -> None:
        self._bot = bot
        self._cancel_handlers: dict[int, callable] = {}  # message_id -> handler
        self._message_listeners: dict[
            int, MessageListener
        ] = {}  # channel_id -> listener

    def add_message_listener(self, channel_id: int, listener: MessageListener) -> None:
        """Add a listener for messages sent to a channel."""
        self._message_listeners[channel_id] = listener

    def remove_message_listener(self, channel_id: int) -> None:
        """Remove a message listener for a channel."""
        self._message_listeners.pop(channel_id, None)

    def register_cancel_handler(self, message_id: int, handler: callable) -> None:
        """Register a cancel handler for a message."""
        self._cancel_handlers[message_id] = handler

    def unregister_cancel_handler(self, message_id: int) -> None:
        """Unregister a cancel handler."""
        self._cancel_handlers.pop(message_id, None)

    async def handle_cancel_interaction(self, interaction: discord.Interaction) -> None:
        """Handle a cancel button interaction."""
        if interaction.message is None:
            await interaction.response.defer()
            return
        handler = self._cancel_handlers.get(interaction.message.id)
        if handler is not None:
            await handler(interaction)
        else:
            await interaction.response.defer()

    @staticmethod
    def _extract_followups(message: RenderedMessage) -> list[RenderedMessage]:
        followups = message.extra.get("followups")
        if not isinstance(followups, list):
            return []
        return [item for item in followups if isinstance(item, RenderedMessage)]

    async def _send_followups(
        self,
        *,
        channel_id: int,
        followups: list[RenderedMessage],
        reply_to_message_id: int | None,
        thread_id: int | None,
    ) -> None:
        for followup in followups:
            show_cancel = followup.extra.get("show_cancel", False)
            view = CancelView() if show_cancel else ClearView()
            await self._bot.send_message(
                channel_id=channel_id,
                content=followup.text,
                reply_to_message_id=reply_to_message_id,
                thread_id=thread_id,
                view=view,
            )

    async def close(self) -> None:
        """Close the transport."""
        await self._bot.close()

    async def send(
        self,
        *,
        channel_id: int | str,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef | None:
        """Send a message."""
        cid = cast(int, channel_id)
        reply_to_message_id: int | None = None
        thread_id: int | None = None

        if options is not None:
            reply_to_message_id = (
                cast(int, options.reply_to.message_id)
                if options.reply_to is not None
                else None
            )
            thread_id = (
                cast(int | None, options.thread_id)
                if options.thread_id is not None
                else None
            )

        show_cancel = message.extra.get("show_cancel", False)
        view = CancelView() if show_cancel else ClearView()

        followups = self._extract_followups(message)
        sent = await self._bot.send_message(
            channel_id=cid,
            content=message.text,
            reply_to_message_id=reply_to_message_id,
            thread_id=thread_id,
            view=view,
        )
        if sent is None:
            return None

        # Delete the old message if replace is specified (mirrors Telegram behavior)
        if options is not None and options.replace is not None:
            await self.delete(ref=options.replace)

        if followups:
            await self._send_followups(
                channel_id=cid,
                followups=followups,
                reply_to_message_id=reply_to_message_id,
                thread_id=thread_id,
            )

        # Notify message listeners (for voice TTS)
        # Check if this is a final message (no cancel button = final response)
        is_final = not show_cancel
        listener = self._message_listeners.get(cid)
        if listener is not None and is_final:
            with contextlib.suppress(Exception):
                await listener(cid, message.text, is_final)

        return MessageRef(
            channel_id=cid,
            message_id=sent.message_id,
            raw=sent,
            thread_id=thread_id or sent.thread_id,
        )

    async def edit(
        self,
        *,
        ref: MessageRef,
        message: RenderedMessage,
        wait: bool = True,
    ) -> MessageRef | None:
        """Edit an existing message."""
        channel_id = cast(int, ref.channel_id)
        message_id = cast(int, ref.message_id)

        show_cancel = message.extra.get("show_cancel", False)
        view = CancelView() if show_cancel else ClearView()

        followups = self._extract_followups(message)
        edited = await self._bot.edit_message(
            channel_id=ref.thread_id or channel_id,
            message_id=message_id,
            content=message.text,
            view=view,
            wait=wait,
        )
        if edited is None:
            return ref if not wait else None

        if followups:
            await self._send_followups(
                channel_id=channel_id,
                followups=followups,
                reply_to_message_id=None,
                thread_id=ref.thread_id,
            )

        return MessageRef(
            channel_id=channel_id,
            message_id=edited.message_id,
            raw=edited,
            thread_id=ref.thread_id,
        )

    async def delete(self, *, ref: MessageRef) -> bool:
        """Delete a message."""
        return await self._bot.delete_message(
            channel_id=cast(int, ref.thread_id or ref.channel_id),
            message_id=cast(int, ref.message_id),
        )
