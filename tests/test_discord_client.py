"""Comprehensive tests for discord/client.py — DiscordBotClient."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from tunapi.discord.client import DEFAULT_CHANNEL_RPS, DiscordBotClient, SentMessage
from tunapi.discord.outbox import RetryAfter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeSleep:
    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


def _make_client(
    *,
    guild_id: int | None = None,
    channel_rps: float = 0.0,
) -> tuple[DiscordBotClient, FakeClock, FakeSleep]:
    clock = FakeClock()
    sleep = FakeSleep(clock)
    client = DiscordBotClient(
        token="test-token",
        guild_id=guild_id,
        channel_rps=channel_rps,
        clock=clock,
        sleep=sleep,
    )
    return client, clock, sleep


def _inject_bot(client: DiscordBotClient) -> MagicMock:
    """Inject a mock bot into the client, bypassing _ensure_bot."""
    bot = MagicMock(spec=discord.Bot)
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(return_value=None)
    bot.get_guild = MagicMock(return_value=None)
    bot.close = AsyncMock()
    bot.user = MagicMock(spec=discord.User)
    bot.user.name = "TestBot"
    bot.user.id = 12345
    client._bot = bot
    return bot


def _make_messageable_channel(channel_id: int = 100) -> MagicMock:
    """Create a mock channel that passes isinstance(channel, discord.abc.Messageable)."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.send = AsyncMock()
    channel.fetch_message = AsyncMock()
    return channel


def _make_sent_discord_message(
    msg_id: int = 999, channel_id: int = 100
) -> MagicMock:
    """Create a mock discord.Message returned by channel.send / channel.fetch_message."""
    msg = MagicMock(spec=discord.Message)
    msg.id = msg_id
    msg.channel = MagicMock()
    msg.channel.id = channel_id
    msg.edit = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def _make_http_exception(status: int = 400, text: str | None = None) -> discord.HTTPException:
    """Create a discord.HTTPException with given status."""
    response = MagicMock()
    response.status = status
    response.headers = {}
    exc = discord.HTTPException.__new__(discord.HTTPException)
    exc.status = status
    exc.code = 0
    exc.text = text or ""
    exc.response = response
    exc.args = (f"HTTP {status}",)
    return exc


# ===========================================================================
# SentMessage dataclass
# ===========================================================================


class TestSentMessage:
    def test_basic_fields(self) -> None:
        sm = SentMessage(message_id=1, channel_id=2)
        assert sm.message_id == 1
        assert sm.channel_id == 2
        assert sm.thread_id is None

    def test_with_thread_id(self) -> None:
        sm = SentMessage(message_id=1, channel_id=2, thread_id=3)
        assert sm.thread_id == 3

    def test_frozen(self) -> None:
        sm = SentMessage(message_id=1, channel_id=2)
        with pytest.raises(AttributeError):
            sm.message_id = 99  # type: ignore[misc]


# ===========================================================================
# Constructor & properties
# ===========================================================================


class TestConstructor:
    def test_defaults(self) -> None:
        client = DiscordBotClient(token="tok")
        assert client._token == "tok"
        assert client._guild_id is None
        assert client._bot is None

    def test_channel_interval_from_rps(self) -> None:
        client = DiscordBotClient(token="t", channel_rps=2.0)
        assert client._channel_interval == pytest.approx(0.5)

    def test_zero_rps_gives_zero_interval(self) -> None:
        client = DiscordBotClient(token="t", channel_rps=0.0)
        assert client._channel_interval == 0.0

    def test_negative_rps_gives_zero_interval(self) -> None:
        client = DiscordBotClient(token="t", channel_rps=-1.0)
        assert client._channel_interval == 0.0

    def test_default_rps_constant(self) -> None:
        assert DEFAULT_CHANNEL_RPS == 1.0


class TestProperties:
    def test_user_none_when_no_bot(self) -> None:
        client, _, _ = _make_client()
        assert client.user is None

    def test_user_returns_bot_user(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        assert client.user is bot.user

    def test_bot_property_creates_bot(self) -> None:
        client, _, _ = _make_client()
        with patch("tunapi.discord.client.discord.Bot"):
            _ = client.bot
            assert client._bot is not None


class TestIntervalForChannel:
    def test_returns_configured_interval(self) -> None:
        client, _, _ = _make_client(channel_rps=5.0)
        assert client.interval_for_channel(123) == pytest.approx(0.2)

    def test_zero_rps(self) -> None:
        client, _, _ = _make_client(channel_rps=0.0)
        assert client.interval_for_channel(None) == 0.0


# ===========================================================================
# Handler setters
# ===========================================================================


class TestHandlers:
    def test_set_message_handler(self) -> None:
        client, _, _ = _make_client()
        handler = AsyncMock()
        client.set_message_handler(handler)
        assert client._message_handler is handler

    def test_set_interaction_handler(self) -> None:
        client, _, _ = _make_client()
        handler = AsyncMock()
        client.set_interaction_handler(handler)
        assert client._interaction_handler is handler


# ===========================================================================
# _unique_key
# ===========================================================================


class TestUniqueKey:
    def test_generates_incrementing_keys(self) -> None:
        client, _, _ = _make_client()
        k1 = client._unique_key("send")
        k2 = client._unique_key("send")
        assert k1[0] == "send"
        assert k2[0] == "send"
        assert k1[1] != k2[1]
        assert k2[1] == k1[1] + 1

    def test_different_prefixes(self) -> None:
        client, _, _ = _make_client()
        k1 = client._unique_key("a")
        k2 = client._unique_key("b")
        assert k1[0] == "a"
        assert k2[0] == "b"


# ===========================================================================
# _extract_retry_after
# ===========================================================================


class TestExtractRetryAfter:
    def test_from_response_header(self) -> None:
        client, _, _ = _make_client()
        exc = _make_http_exception(429)
        exc.response.headers["Retry-After"] = "2.5"
        assert client._extract_retry_after(exc) == pytest.approx(2.5)

    def test_from_json_body(self) -> None:
        client, _, _ = _make_client()
        exc = _make_http_exception(429, text=json.dumps({"retry_after": 3.0}))
        exc.response.headers = {}
        assert client._extract_retry_after(exc) == pytest.approx(3.0)

    def test_fallback_default(self) -> None:
        client, _, _ = _make_client()
        exc = _make_http_exception(429)
        exc.response.headers = {}
        exc.text = ""
        assert client._extract_retry_after(exc) == pytest.approx(1.0)

    def test_invalid_header_falls_to_body(self) -> None:
        client, _, _ = _make_client()
        exc = _make_http_exception(429, text=json.dumps({"retry_after": 4.0}))
        exc.response.headers["Retry-After"] = "not-a-number"
        assert client._extract_retry_after(exc) == pytest.approx(4.0)

    def test_no_response_attr(self) -> None:
        client, _, _ = _make_client()
        exc = _make_http_exception(429)
        exc.response = None
        exc.text = ""
        assert client._extract_retry_after(exc) == pytest.approx(1.0)

    def test_invalid_json_body(self) -> None:
        client, _, _ = _make_client()
        exc = _make_http_exception(429, text="not json")
        exc.response.headers = {}
        assert client._extract_retry_after(exc) == pytest.approx(1.0)


# ===========================================================================
# _send_message_impl
# ===========================================================================


class TestSendMessageImpl:
    @pytest.mark.anyio
    async def test_basic_send(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        sent_msg = _make_sent_discord_message(999, 100)
        channel.send.return_value = sent_msg
        bot.get_channel.return_value = channel

        result = await client._send_message_impl(
            channel_id=100, content="hello"
        )
        assert result is not None
        assert result.message_id == 999
        assert result.channel_id == 100
        channel.send.assert_awaited_once()
        kwargs = channel.send.call_args.kwargs
        assert kwargs["content"] == "hello"
        assert kwargs["suppress"] is True

    @pytest.mark.anyio
    async def test_send_with_reply(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        sent_msg = _make_sent_discord_message(999, 100)
        channel.send.return_value = sent_msg
        bot.get_channel.return_value = channel

        result = await client._send_message_impl(
            channel_id=100, content="reply", reply_to_message_id=555
        )
        assert result is not None
        kwargs = channel.send.call_args.kwargs
        assert "reference" in kwargs
        ref = kwargs["reference"]
        assert ref.message_id == 555
        assert ref.channel_id == 100

    @pytest.mark.anyio
    async def test_send_with_thread_id(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(200)
        sent_msg = _make_sent_discord_message(999, 200)
        channel.send.return_value = sent_msg
        bot.get_channel.return_value = channel

        result = await client._send_message_impl(
            channel_id=100, content="in thread", thread_id=200
        )
        assert result is not None
        assert result.thread_id == 200
        # Should look up thread_id, not channel_id
        bot.get_channel.assert_called_with(200)

    @pytest.mark.anyio
    async def test_send_with_view_and_embed(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        sent_msg = _make_sent_discord_message(999, 100)
        channel.send.return_value = sent_msg
        bot.get_channel.return_value = channel
        view = MagicMock(spec=discord.ui.View)
        embed = MagicMock(spec=discord.Embed)

        result = await client._send_message_impl(
            channel_id=100, content="fancy", view=view, embed=embed
        )
        assert result is not None
        kwargs = channel.send.call_args.kwargs
        assert kwargs["view"] is view
        assert kwargs["embed"] is embed

    @pytest.mark.anyio
    async def test_send_channel_not_found_get(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        bot.get_channel.return_value = None
        bot.fetch_channel.side_effect = discord.NotFound(MagicMock(), "not found")

        result = await client._send_message_impl(channel_id=100, content="hi")
        assert result is None

    @pytest.mark.anyio
    async def test_send_channel_not_messageable(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        # Return a channel that is NOT Messageable
        non_msg = MagicMock(spec=discord.CategoryChannel)
        bot.get_channel.return_value = non_msg

        result = await client._send_message_impl(channel_id=100, content="hi")
        assert result is None

    @pytest.mark.anyio
    async def test_send_fetch_fallback(self) -> None:
        """When get_channel returns None, fetch_channel is used."""
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        sent_msg = _make_sent_discord_message(999, 100)
        channel.send.return_value = sent_msg
        bot.get_channel.return_value = None
        bot.fetch_channel.return_value = channel

        result = await client._send_message_impl(channel_id=100, content="hi")
        assert result is not None
        bot.fetch_channel.assert_awaited_once_with(100)

    @pytest.mark.anyio
    async def test_send_http_error_returns_none(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        channel.send.side_effect = _make_http_exception(500)
        bot.get_channel.return_value = channel

        result = await client._send_message_impl(channel_id=100, content="hi")
        assert result is None

    @pytest.mark.anyio
    async def test_send_429_raises_retry_after(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        exc_429 = _make_http_exception(429, text=json.dumps({"retry_after": 5.0}))
        exc_429.response.headers = {}
        channel.send.side_effect = exc_429
        bot.get_channel.return_value = channel

        with pytest.raises(RetryAfter) as exc_info:
            await client._send_message_impl(channel_id=100, content="hi")
        assert exc_info.value.retry_after == pytest.approx(5.0)

    @pytest.mark.anyio
    async def test_send_http_error_with_reference_retries_without(self) -> None:
        """On HTTPException with reference, retries without reference."""
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        sent_msg = _make_sent_discord_message(999, 100)
        # First call fails (with reference), second succeeds (without)
        channel.send.side_effect = [_make_http_exception(400), sent_msg]
        bot.get_channel.return_value = channel

        result = await client._send_message_impl(
            channel_id=100, content="hi", reply_to_message_id=555
        )
        assert result is not None
        assert result.message_id == 999
        assert channel.send.await_count == 2
        # Second call should not have reference
        second_call_kwargs = channel.send.call_args_list[1].kwargs
        assert "reference" not in second_call_kwargs

    @pytest.mark.anyio
    async def test_send_retry_without_reference_also_fails(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        channel.send.side_effect = [_make_http_exception(400), _make_http_exception(500)]
        bot.get_channel.return_value = channel

        result = await client._send_message_impl(
            channel_id=100, content="hi", reply_to_message_id=555
        )
        assert result is None

    @pytest.mark.anyio
    async def test_send_retry_without_reference_429(self) -> None:
        """429 on the retry-without-reference path also raises RetryAfter."""
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        exc_429 = _make_http_exception(429, text=json.dumps({"retry_after": 2.0}))
        exc_429.response.headers = {}
        channel.send.side_effect = [_make_http_exception(400), exc_429]
        bot.get_channel.return_value = channel

        with pytest.raises(RetryAfter) as exc_info:
            await client._send_message_impl(
                channel_id=100, content="hi", reply_to_message_id=555
            )
        assert exc_info.value.retry_after == pytest.approx(2.0)

    @pytest.mark.anyio
    async def test_send_suppress_embeds_false(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        sent_msg = _make_sent_discord_message(999, 100)
        channel.send.return_value = sent_msg
        bot.get_channel.return_value = channel

        await client._send_message_impl(
            channel_id=100, content="hi", suppress_embeds=False
        )
        kwargs = channel.send.call_args.kwargs
        assert kwargs["suppress"] is False


# ===========================================================================
# _edit_message_impl
# ===========================================================================


class TestEditMessageImpl:
    @pytest.mark.anyio
    async def test_basic_edit(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        original_msg = _make_sent_discord_message(50, 100)
        edited_msg = _make_sent_discord_message(50, 100)
        original_msg.edit.return_value = edited_msg
        channel.fetch_message.return_value = original_msg
        bot.get_channel.return_value = channel

        result = await client._edit_message_impl(
            channel_id=100, message_id=50, content="updated"
        )
        assert result is not None
        assert result.message_id == 50
        original_msg.edit.assert_awaited_once()
        kwargs = original_msg.edit.call_args.kwargs
        assert kwargs["content"] == "updated"
        assert kwargs["suppress"] is True

    @pytest.mark.anyio
    async def test_edit_with_view_and_embed(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        original_msg = _make_sent_discord_message(50, 100)
        edited_msg = _make_sent_discord_message(50, 100)
        original_msg.edit.return_value = edited_msg
        channel.fetch_message.return_value = original_msg
        bot.get_channel.return_value = channel
        view = MagicMock(spec=discord.ui.View)
        embed = MagicMock(spec=discord.Embed)

        result = await client._edit_message_impl(
            channel_id=100, message_id=50, content="up", view=view, embed=embed
        )
        assert result is not None
        kwargs = original_msg.edit.call_args.kwargs
        assert kwargs["view"] is view
        assert kwargs["embed"] is embed

    @pytest.mark.anyio
    async def test_edit_channel_not_found(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        bot.get_channel.return_value = None
        bot.fetch_channel.side_effect = discord.NotFound(MagicMock(), "nf")

        result = await client._edit_message_impl(
            channel_id=100, message_id=50, content="up"
        )
        assert result is None

    @pytest.mark.anyio
    async def test_edit_channel_not_messageable(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        bot.get_channel.return_value = MagicMock(spec=discord.CategoryChannel)

        result = await client._edit_message_impl(
            channel_id=100, message_id=50, content="up"
        )
        assert result is None

    @pytest.mark.anyio
    async def test_edit_http_error_returns_none(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        channel.fetch_message.side_effect = _make_http_exception(403)
        bot.get_channel.return_value = channel

        result = await client._edit_message_impl(
            channel_id=100, message_id=50, content="up"
        )
        assert result is None

    @pytest.mark.anyio
    async def test_edit_429_raises_retry_after(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        exc_429 = _make_http_exception(429, text=json.dumps({"retry_after": 1.5}))
        exc_429.response.headers = {}
        channel.fetch_message.side_effect = exc_429
        bot.get_channel.return_value = channel

        with pytest.raises(RetryAfter) as exc_info:
            await client._edit_message_impl(
                channel_id=100, message_id=50, content="up"
            )
        assert exc_info.value.retry_after == pytest.approx(1.5)

    @pytest.mark.anyio
    async def test_edit_fetch_channel_fallback(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        original_msg = _make_sent_discord_message(50, 100)
        edited_msg = _make_sent_discord_message(50, 100)
        original_msg.edit.return_value = edited_msg
        channel.fetch_message.return_value = original_msg
        bot.get_channel.return_value = None
        bot.fetch_channel.return_value = channel

        result = await client._edit_message_impl(
            channel_id=100, message_id=50, content="up"
        )
        assert result is not None
        bot.fetch_channel.assert_awaited_once_with(100)


# ===========================================================================
# _delete_message_impl
# ===========================================================================


class TestDeleteMessageImpl:
    @pytest.mark.anyio
    async def test_basic_delete(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        msg = _make_sent_discord_message(50, 100)
        channel.fetch_message.return_value = msg
        bot.get_channel.return_value = channel

        result = await client._delete_message_impl(channel_id=100, message_id=50)
        assert result is True
        msg.delete.assert_awaited_once()

    @pytest.mark.anyio
    async def test_delete_channel_not_found(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        bot.get_channel.return_value = None
        bot.fetch_channel.side_effect = discord.NotFound(MagicMock(), "nf")

        result = await client._delete_message_impl(channel_id=100, message_id=50)
        assert result is False

    @pytest.mark.anyio
    async def test_delete_channel_not_messageable(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        bot.get_channel.return_value = MagicMock(spec=discord.CategoryChannel)

        result = await client._delete_message_impl(channel_id=100, message_id=50)
        assert result is False

    @pytest.mark.anyio
    async def test_delete_http_error_returns_false(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        channel.fetch_message.side_effect = _make_http_exception(403)
        bot.get_channel.return_value = channel

        result = await client._delete_message_impl(channel_id=100, message_id=50)
        assert result is False

    @pytest.mark.anyio
    async def test_delete_429_raises_retry_after(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        exc_429 = _make_http_exception(429, text=json.dumps({"retry_after": 3.0}))
        exc_429.response.headers = {}
        channel.fetch_message.side_effect = exc_429
        bot.get_channel.return_value = channel

        with pytest.raises(RetryAfter) as exc_info:
            await client._delete_message_impl(channel_id=100, message_id=50)
        assert exc_info.value.retry_after == pytest.approx(3.0)

    @pytest.mark.anyio
    async def test_delete_fetch_channel_fallback(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = _make_messageable_channel(100)
        msg = _make_sent_discord_message(50, 100)
        channel.fetch_message.return_value = msg
        bot.get_channel.return_value = None
        bot.fetch_channel.return_value = channel

        result = await client._delete_message_impl(channel_id=100, message_id=50)
        assert result is True
        bot.fetch_channel.assert_awaited_once_with(100)


# ===========================================================================
# create_thread (from message)
# ===========================================================================


class TestCreateThread:
    @pytest.mark.anyio
    async def test_basic_create_thread(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 100
        msg = MagicMock(spec=discord.Message)
        thread = MagicMock(spec=discord.Thread)
        thread.id = 777
        thread.join = AsyncMock()
        msg.create_thread = AsyncMock(return_value=thread)
        channel.fetch_message = AsyncMock(return_value=msg)
        bot.get_channel.return_value = channel

        result = await client.create_thread(
            channel_id=100, message_id=50, name="Test Thread"
        )
        assert result == 777
        msg.create_thread.assert_awaited_once_with(
            name="Test Thread", auto_archive_duration=1440
        )
        thread.join.assert_awaited_once()

    @pytest.mark.anyio
    async def test_create_thread_custom_archive_duration(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = MagicMock(spec=discord.TextChannel)
        msg = MagicMock(spec=discord.Message)
        thread = MagicMock(spec=discord.Thread)
        thread.id = 777
        thread.join = AsyncMock()
        msg.create_thread = AsyncMock(return_value=thread)
        channel.fetch_message = AsyncMock(return_value=msg)
        bot.get_channel.return_value = channel

        await client.create_thread(
            channel_id=100, message_id=50, name="T", auto_archive_duration=60
        )
        msg.create_thread.assert_awaited_once_with(name="T", auto_archive_duration=60)

    @pytest.mark.anyio
    async def test_create_thread_channel_not_found(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        bot.get_channel.return_value = None
        bot.fetch_channel.side_effect = discord.NotFound(MagicMock(), "nf")

        result = await client.create_thread(
            channel_id=100, message_id=50, name="T"
        )
        assert result is None

    @pytest.mark.anyio
    async def test_create_thread_not_text_channel(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        bot.get_channel.return_value = MagicMock(spec=discord.VoiceChannel)

        result = await client.create_thread(
            channel_id=100, message_id=50, name="T"
        )
        assert result is None

    @pytest.mark.anyio
    async def test_create_thread_http_error(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(side_effect=_make_http_exception(500))
        bot.get_channel.return_value = channel

        result = await client.create_thread(
            channel_id=100, message_id=50, name="T"
        )
        assert result is None


# ===========================================================================
# create_thread_without_message
# ===========================================================================


class TestCreateThreadWithoutMessage:
    @pytest.mark.anyio
    async def test_basic_create(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 100
        thread = MagicMock(spec=discord.Thread)
        thread.id = 888
        thread.join = AsyncMock()
        channel.create_thread = AsyncMock(return_value=thread)
        bot.get_channel.return_value = channel

        result = await client.create_thread_without_message(
            channel_id=100, name="No-msg thread"
        )
        assert result == 888
        channel.create_thread.assert_awaited_once_with(
            name="No-msg thread",
            message=None,
            auto_archive_duration=1440,
            type=discord.ChannelType.public_thread,
        )
        thread.join.assert_awaited_once()

    @pytest.mark.anyio
    async def test_channel_not_found(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        bot.get_channel.return_value = None
        bot.fetch_channel.side_effect = discord.NotFound(MagicMock(), "nf")

        result = await client.create_thread_without_message(
            channel_id=100, name="T"
        )
        assert result is None

    @pytest.mark.anyio
    async def test_not_text_channel(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        bot.get_channel.return_value = MagicMock(spec=discord.VoiceChannel)

        result = await client.create_thread_without_message(
            channel_id=100, name="T"
        )
        assert result is None

    @pytest.mark.anyio
    async def test_http_error(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = MagicMock(spec=discord.TextChannel)
        channel.create_thread = AsyncMock(side_effect=_make_http_exception(500))
        bot.get_channel.return_value = channel

        result = await client.create_thread_without_message(
            channel_id=100, name="T"
        )
        assert result is None

    @pytest.mark.anyio
    async def test_fetch_channel_fallback(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = MagicMock(spec=discord.TextChannel)
        thread = MagicMock(spec=discord.Thread)
        thread.id = 888
        thread.join = AsyncMock()
        channel.create_thread = AsyncMock(return_value=thread)
        bot.get_channel.return_value = None
        bot.fetch_channel.return_value = channel

        result = await client.create_thread_without_message(
            channel_id=100, name="T"
        )
        assert result == 888
        bot.fetch_channel.assert_awaited_once_with(100)


# ===========================================================================
# get_guild / get_channel
# ===========================================================================


class TestGetGuild:
    def test_returns_guild(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        guild = MagicMock(spec=discord.Guild)
        bot.get_guild.return_value = guild
        assert client.get_guild(42) is guild
        bot.get_guild.assert_called_with(42)

    def test_returns_none(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        bot.get_guild.return_value = None
        assert client.get_guild(42) is None


class TestGetChannel:
    def test_returns_guild_channel(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = MagicMock(spec=discord.TextChannel)
        bot.get_channel.return_value = channel
        assert client.get_channel(42) is channel

    def test_returns_none_for_non_guild_channel(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        channel = MagicMock(spec=discord.DMChannel)
        bot.get_channel.return_value = channel
        assert client.get_channel(42) is None

    def test_returns_none_when_not_found(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        bot.get_channel.return_value = None
        assert client.get_channel(42) is None


# ===========================================================================
# Queued public methods (send_message, edit_message, delete_message)
# These test that the public methods correctly delegate to the outbox.
# ===========================================================================


class TestSendMessageQueued:
    @pytest.mark.anyio
    async def test_send_enqueues_and_returns(self) -> None:
        client, _, _ = _make_client()
        _inject_bot(client)
        expected = SentMessage(message_id=1, channel_id=100)
        client._outbox = MagicMock()
        client._outbox.enqueue = AsyncMock(return_value=expected)

        result = await client.send_message(channel_id=100, content="hi")
        assert result is expected
        client._outbox.enqueue.assert_awaited_once()
        call_kwargs = client._outbox.enqueue.call_args.kwargs
        assert call_kwargs["op"].label == "send_message"
        assert call_kwargs["op"].priority == 0  # SEND_PRIORITY

    @pytest.mark.anyio
    async def test_send_uses_thread_id_as_channel(self) -> None:
        """When thread_id is set, outbox channel_id should be the thread_id."""
        client, _, _ = _make_client()
        _inject_bot(client)
        client._outbox = MagicMock()
        client._outbox.enqueue = AsyncMock(return_value=None)

        await client.send_message(channel_id=100, content="hi", thread_id=200)
        call_kwargs = client._outbox.enqueue.call_args.kwargs
        assert call_kwargs["op"].channel_id == 200


class TestEditMessageQueued:
    @pytest.mark.anyio
    async def test_edit_enqueues_with_coalescing_key(self) -> None:
        client, _, _ = _make_client()
        _inject_bot(client)
        client._outbox = MagicMock()
        client._outbox.enqueue = AsyncMock(return_value=None)

        await client.edit_message(
            channel_id=100, message_id=50, content="updated"
        )
        call_kwargs = client._outbox.enqueue.call_args.kwargs
        assert call_kwargs["key"] == ("edit", 100, 50)
        assert call_kwargs["op"].label == "edit_message"
        assert call_kwargs["op"].priority == 2  # EDIT_PRIORITY

    @pytest.mark.anyio
    async def test_edit_wait_false(self) -> None:
        client, _, _ = _make_client()
        _inject_bot(client)
        client._outbox = MagicMock()
        client._outbox.enqueue = AsyncMock(return_value=None)

        await client.edit_message(
            channel_id=100, message_id=50, content="up", wait=False
        )
        call_kwargs = client._outbox.enqueue.call_args.kwargs
        assert call_kwargs["wait"] is False


class TestDeleteMessageQueued:
    @pytest.mark.anyio
    async def test_delete_drops_pending_edits_first(self) -> None:
        client, _, _ = _make_client()
        _inject_bot(client)
        client._outbox = MagicMock()
        client._outbox.enqueue = AsyncMock(return_value=True)
        client._outbox.drop_pending = AsyncMock()

        result = await client.delete_message(channel_id=100, message_id=50)
        assert result is True
        client._outbox.drop_pending.assert_awaited_once_with(
            key=("edit", 100, 50)
        )

    @pytest.mark.anyio
    async def test_delete_enqueues_with_priority(self) -> None:
        client, _, _ = _make_client()
        _inject_bot(client)
        client._outbox = MagicMock()
        client._outbox.enqueue = AsyncMock(return_value=True)
        client._outbox.drop_pending = AsyncMock()

        await client.delete_message(channel_id=100, message_id=50)
        call_kwargs = client._outbox.enqueue.call_args.kwargs
        assert call_kwargs["key"] == ("delete", 100, 50)
        assert call_kwargs["op"].priority == 1  # DELETE_PRIORITY

    @pytest.mark.anyio
    async def test_delete_returns_false_on_none(self) -> None:
        client, _, _ = _make_client()
        _inject_bot(client)
        client._outbox = MagicMock()
        client._outbox.enqueue = AsyncMock(return_value=None)
        client._outbox.drop_pending = AsyncMock()

        result = await client.delete_message(channel_id=100, message_id=50)
        assert result is False


# ===========================================================================
# drop_pending_edits
# ===========================================================================


class TestDropPendingEdits:
    @pytest.mark.anyio
    async def test_delegates_to_outbox(self) -> None:
        client, _, _ = _make_client()
        client._outbox = MagicMock()
        client._outbox.drop_pending = AsyncMock()

        await client.drop_pending_edits(channel_id=100, message_id=50)
        client._outbox.drop_pending.assert_awaited_once_with(
            key=("edit", 100, 50)
        )


# ===========================================================================
# close
# ===========================================================================


class TestClose:
    @pytest.mark.anyio
    async def test_close_no_bot(self) -> None:
        """Close with no bot created should not raise."""
        client, _, _ = _make_client()
        client._outbox = MagicMock()
        client._outbox.close = AsyncMock()
        await client.close()
        client._outbox.close.assert_awaited_once()

    @pytest.mark.anyio
    async def test_close_with_bot(self) -> None:
        client, _, _ = _make_client()
        bot = _inject_bot(client)
        client._outbox = MagicMock()
        client._outbox.close = AsyncMock()
        await client.close()
        client._outbox.close.assert_awaited_once()
        bot.close.assert_awaited_once()


# ===========================================================================
# _log_request_error / _log_outbox_failure
# ===========================================================================


class TestLogging:
    def test_log_request_error(self) -> None:
        client, _, _ = _make_client()
        op = MagicMock()
        op.label = "send_message"
        # Should not raise
        client._log_request_error(op, ValueError("boom"))

    def test_log_outbox_failure(self) -> None:
        client, _, _ = _make_client()
        client._log_outbox_failure(RuntimeError("crash"))
