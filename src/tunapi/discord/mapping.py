"""Category/channel to project mapping."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .client import DiscordBotClient


@dataclass(frozen=True, slots=True)
class ChannelMapping:
    """Mapping of a Discord channel to a project."""

    guild_id: int
    category_id: int | None
    category_name: str | None
    channel_id: int
    channel_name: str


class CategoryChannelMapper:
    """Maps Discord categories and channels to projects.

    Note: This class now only provides channel metadata.
    Project binding is managed through the state store via /bind command.
    Branch selection is done via @branch prefix which creates threads.
    """

    def __init__(self, bot: DiscordBotClient) -> None:
        self._bot = bot

    def get_channel_mapping(
        self,
        guild_id: int,
        channel_id: int,
    ) -> ChannelMapping | None:
        """Get the mapping for a channel (metadata only)."""
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            return None

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None

        # For threads, get the parent channel
        if isinstance(channel, discord.Thread):
            parent = channel.parent
            if parent is None or not isinstance(parent, discord.TextChannel):
                return None
            channel = parent

        category = channel.category
        category_id = category.id if category else None
        category_name = category.name if category else None

        return ChannelMapping(
            guild_id=guild_id,
            category_id=category_id,
            category_name=category_name,
            channel_id=channel.id,
            channel_name=channel.name,
        )

    def list_category_channels(
        self,
        guild_id: int,
        category_id: int,
    ) -> list[ChannelMapping]:
        """List all channels in a category."""
        guild = self._bot.get_guild(guild_id)
        if guild is None:
            return []

        category = guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            return []

        return [
            ChannelMapping(
                guild_id=guild_id,
                category_id=category_id,
                category_name=category.name,
                channel_id=channel.id,
                channel_name=channel.name,
            )
            for channel in category.channels
            if isinstance(channel, discord.TextChannel)
        ]
