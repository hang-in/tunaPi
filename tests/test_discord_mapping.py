"""Tests for discord/mapping.py — ChannelMapping and CategoryChannelMapper."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import discord

from tunapi.discord.mapping import CategoryChannelMapper, ChannelMapping


# ---------------------------------------------------------------------------
# ChannelMapping dataclass
# ---------------------------------------------------------------------------


class TestChannelMapping:
    def test_fields(self) -> None:
        m = ChannelMapping(
            guild_id=1,
            category_id=2,
            category_name="Category",
            channel_id=3,
            channel_name="general",
        )
        assert m.guild_id == 1
        assert m.category_id == 2
        assert m.category_name == "Category"
        assert m.channel_id == 3
        assert m.channel_name == "general"

    def test_frozen(self) -> None:
        m = ChannelMapping(
            guild_id=1,
            category_id=None,
            category_name=None,
            channel_id=3,
            channel_name="general",
        )
        try:
            m.guild_id = 99  # type: ignore[misc]
            raise AssertionError("Should have raised")
        except AttributeError:
            pass

    def test_none_category(self) -> None:
        m = ChannelMapping(
            guild_id=1,
            category_id=None,
            category_name=None,
            channel_id=3,
            channel_name="general",
        )
        assert m.category_id is None
        assert m.category_name is None

    def test_equality(self) -> None:
        a = ChannelMapping(1, 2, "Cat", 3, "ch")
        b = ChannelMapping(1, 2, "Cat", 3, "ch")
        assert a == b

    def test_inequality(self) -> None:
        a = ChannelMapping(1, 2, "Cat", 3, "ch")
        b = ChannelMapping(1, 2, "Cat", 4, "ch")
        assert a != b


# ---------------------------------------------------------------------------
# Helpers — lightweight fakes for discord.py objects
# ---------------------------------------------------------------------------


def _make_text_channel(
    channel_id: int,
    name: str,
    category: object | None = None,
) -> MagicMock:
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    ch.name = name
    ch.category = category
    return ch


def _make_thread(
    thread_id: int,
    name: str,
    parent: object | None,
) -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = thread_id
    t.name = name
    t.parent = parent
    return t


def _make_category(
    category_id: int,
    name: str,
    channels: list[object] | None = None,
) -> MagicMock:
    cat = MagicMock(spec=discord.CategoryChannel)
    cat.id = category_id
    cat.name = name
    cat.channels = channels or []
    return cat


def _make_bot() -> MagicMock:
    return MagicMock(spec=["get_channel", "get_guild"])


# ---------------------------------------------------------------------------
# get_channel_mapping
# ---------------------------------------------------------------------------


class TestGetChannelMapping:
    def test_channel_not_found(self) -> None:
        bot = _make_bot()
        bot.get_channel.return_value = None
        mapper = CategoryChannelMapper(bot)
        assert mapper.get_channel_mapping(guild_id=1, channel_id=99) is None

    def test_unsupported_channel_type(self) -> None:
        """Voice channel or other non-text types should return None."""
        bot = _make_bot()
        vc = MagicMock(spec=discord.VoiceChannel)
        bot.get_channel.return_value = vc
        mapper = CategoryChannelMapper(bot)
        assert mapper.get_channel_mapping(guild_id=1, channel_id=10) is None

    def test_text_channel_with_category(self) -> None:
        cat = _make_category(20, "Projects")
        ch = _make_text_channel(10, "general", category=cat)
        bot = _make_bot()
        bot.get_channel.return_value = ch

        result = CategoryChannelMapper(bot).get_channel_mapping(
            guild_id=1, channel_id=10
        )
        assert result is not None
        assert result == ChannelMapping(
            guild_id=1,
            category_id=20,
            category_name="Projects",
            channel_id=10,
            channel_name="general",
        )

    def test_text_channel_without_category(self) -> None:
        ch = _make_text_channel(10, "general", category=None)
        bot = _make_bot()
        bot.get_channel.return_value = ch

        result = CategoryChannelMapper(bot).get_channel_mapping(
            guild_id=1, channel_id=10
        )
        assert result is not None
        assert result.category_id is None
        assert result.category_name is None
        assert result.channel_id == 10
        assert result.channel_name == "general"

    def test_thread_resolves_to_parent(self) -> None:
        cat = _make_category(20, "Dev")
        parent_ch = _make_text_channel(10, "backend", category=cat)
        thread = _make_thread(30, "my-thread", parent=parent_ch)
        bot = _make_bot()
        bot.get_channel.return_value = thread

        result = CategoryChannelMapper(bot).get_channel_mapping(
            guild_id=1, channel_id=30
        )
        assert result is not None
        # Should use parent channel's id/name, not thread's
        assert result.channel_id == 10
        assert result.channel_name == "backend"
        assert result.category_id == 20
        assert result.category_name == "Dev"

    def test_thread_with_none_parent(self) -> None:
        thread = _make_thread(30, "orphan", parent=None)
        bot = _make_bot()
        bot.get_channel.return_value = thread

        assert (
            CategoryChannelMapper(bot).get_channel_mapping(guild_id=1, channel_id=30)
            is None
        )

    def test_thread_with_non_text_parent(self) -> None:
        """Thread whose parent is a forum channel (not TextChannel)."""
        forum = MagicMock(spec=discord.ForumChannel)
        thread = _make_thread(30, "forum-thread", parent=forum)
        bot = _make_bot()
        bot.get_channel.return_value = thread

        assert (
            CategoryChannelMapper(bot).get_channel_mapping(guild_id=1, channel_id=30)
            is None
        )

    def test_thread_parent_without_category(self) -> None:
        parent_ch = _make_text_channel(10, "no-cat", category=None)
        thread = _make_thread(30, "t", parent=parent_ch)
        bot = _make_bot()
        bot.get_channel.return_value = thread

        result = CategoryChannelMapper(bot).get_channel_mapping(
            guild_id=1, channel_id=30
        )
        assert result is not None
        assert result.category_id is None
        assert result.category_name is None
        assert result.channel_id == 10


# ---------------------------------------------------------------------------
# list_category_channels
# ---------------------------------------------------------------------------


class TestListCategoryChannels:
    def test_guild_not_found(self) -> None:
        bot = _make_bot()
        bot.get_guild.return_value = None
        mapper = CategoryChannelMapper(bot)
        assert mapper.list_category_channels(guild_id=1, category_id=20) == []

    def test_category_not_found(self) -> None:
        guild = MagicMock()
        guild.get_channel.return_value = None
        bot = _make_bot()
        bot.get_guild.return_value = guild

        assert CategoryChannelMapper(bot).list_category_channels(1, 20) == []

    def test_channel_is_not_category(self) -> None:
        """get_channel returns something that is not a CategoryChannel."""
        guild = MagicMock()
        guild.get_channel.return_value = MagicMock(spec=discord.TextChannel)
        bot = _make_bot()
        bot.get_guild.return_value = guild

        assert CategoryChannelMapper(bot).list_category_channels(1, 20) == []

    def test_returns_text_channels_only(self) -> None:
        text1 = _make_text_channel(10, "general")
        text2 = _make_text_channel(11, "dev")
        voice = MagicMock(spec=discord.VoiceChannel)
        voice.id = 12
        voice.name = "voice"

        cat = _make_category(20, "MyCategory", channels=[text1, voice, text2])

        guild = MagicMock()
        guild.get_channel.return_value = cat
        bot = _make_bot()
        bot.get_guild.return_value = guild

        results = CategoryChannelMapper(bot).list_category_channels(
            guild_id=1, category_id=20
        )
        assert len(results) == 2
        assert results[0] == ChannelMapping(1, 20, "MyCategory", 10, "general")
        assert results[1] == ChannelMapping(1, 20, "MyCategory", 11, "dev")

    def test_empty_category(self) -> None:
        cat = _make_category(20, "Empty", channels=[])
        guild = MagicMock()
        guild.get_channel.return_value = cat
        bot = _make_bot()
        bot.get_guild.return_value = guild

        assert CategoryChannelMapper(bot).list_category_channels(1, 20) == []
