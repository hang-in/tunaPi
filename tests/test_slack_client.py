from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import anyio

from tunapi.slack.client import SlackClient
from tunapi.slack.api_models import (
    AuthTestResponse,
    ChatPostMessageResponse,
    SlackChannel,
    SlackUser,
)
from tunapi.slack.client_api import SlackRetryAfter

pytestmark = pytest.mark.anyio


@pytest.fixture
def mock_http_client():
    with patch("tunapi.slack.client.HttpSlackClient") as mock_cls:
        client_inst = mock_cls.return_value
        client_inst.auth_test = AsyncMock()
        client_inst.get_user_info = AsyncMock()
        client_inst.get_channel_info = AsyncMock()
        client_inst.post_message = AsyncMock()
        client_inst.update_message = AsyncMock()
        client_inst.delete_message = AsyncMock()
        client_inst.add_reaction = AsyncMock()
        client_inst.get_upload_url = AsyncMock()
        client_inst.upload_content = AsyncMock()
        client_inst.complete_upload_external = AsyncMock()
        client_inst.close = AsyncMock()
        client_inst.socket_mode_connect = MagicMock()
        yield client_inst


async def test_client_init_and_close(mock_http_client):
    client = SlackClient("bot-token", "app-token")
    await client.close()
    mock_http_client.close.assert_called_once()


async def test_auth_test(mock_http_client):
    expected = AuthTestResponse(ok=True, user_id="U1", bot_id="B1")
    mock_http_client.auth_test.return_value = expected

    client = SlackClient("bot-token")
    res = await client.auth_test()
    assert res == expected
    mock_http_client.auth_test.assert_called_once()


async def test_get_user(mock_http_client):
    expected = SlackUser(id="U1", name="alice")
    mock_http_client.get_user_info.return_value = expected

    client = SlackClient("bot-token")
    res = await client.get_user("U1")
    assert res == expected
    mock_http_client.get_user_info.assert_called_once_with("U1")


async def test_get_channel(mock_http_client):
    expected = SlackChannel(id="C1", name="general")
    mock_http_client.get_channel_info.return_value = expected

    client = SlackClient("bot-token")
    res = await client.get_channel("C1")
    assert res == expected
    mock_http_client.get_channel_info.assert_called_once_with("C1")


async def test_send_message(mock_http_client):
    expected = ChatPostMessageResponse(ok=True, channel="C1", ts="123.456")
    mock_http_client.post_message.return_value = expected

    client = SlackClient("bot-token", rps=0)
    res = await client.send_message("C1", "hello", thread_ts="111")
    assert res == expected
    mock_http_client.post_message.assert_called_once_with(
        "C1", "hello", thread_ts="111"
    )


async def test_edit_message(mock_http_client):
    expected = ChatPostMessageResponse(ok=True, channel="C1", ts="123.456")
    mock_http_client.update_message.return_value = expected

    client = SlackClient("bot-token", rps=0)
    res = await client.edit_message("C1", "123.456", "edited text")
    assert res == expected
    mock_http_client.update_message.assert_called_once_with(
        "C1", "123.456", "edited text"
    )


async def test_delete_message(mock_http_client):
    mock_result = MagicMock()
    mock_result.ok = True
    mock_http_client.delete_message.return_value = mock_result

    client = SlackClient("bot-token", rps=0)
    res = await client.delete_message("C1", "123.456")
    assert res is True
    mock_http_client.delete_message.assert_called_once_with("C1", "123.456")


async def test_add_reaction(mock_http_client):
    mock_result = MagicMock()
    mock_result.ok = True
    mock_http_client.add_reaction.return_value = mock_result

    client = SlackClient("bot-token", rps=0)
    res = await client.add_reaction("C1", "123.456", "thumbsup")
    assert res is True
    mock_http_client.add_reaction.assert_called_once_with("C1", "123.456", "thumbsup")


async def test_upload_file_success(mock_http_client):
    mock_get_url = MagicMock()
    mock_get_url.ok = True
    mock_get_url.upload_url = "https://upload.slack/123"
    mock_get_url.file_id = "F123"
    mock_http_client.get_upload_url.return_value = mock_get_url

    mock_complete = MagicMock()
    mock_complete.ok = True
    mock_http_client.complete_upload_external.return_value = mock_complete

    client = SlackClient("bot-token", rps=0)
    res = await client.upload_file(
        "test.txt", b"file content", channel_id="C1", thread_ts="111"
    )
    assert res == "F123"

    mock_http_client.get_upload_url.assert_called_once_with(
        "test.txt", len(b"file content")
    )
    mock_http_client.upload_content.assert_called_once_with(
        "https://upload.slack/123", b"file content"
    )
    mock_http_client.complete_upload_external.assert_called_once_with(
        "F123", channel_id="C1", thread_ts="111"
    )


async def test_upload_file_fail_get_url(mock_http_client):
    mock_get_url = MagicMock()
    mock_get_url.ok = False
    mock_http_client.get_upload_url.return_value = mock_get_url

    client = SlackClient("bot-token", rps=0)
    res = await client.upload_file("test.txt", b"file content")
    assert res is None


async def test_call_with_retry_logic(mock_http_client):
    mock_http_client.auth_test.side_effect = [
        SlackRetryAfter(0.01),
        AuthTestResponse(ok=True, user_id="U1", bot_id="B1"),
    ]

    sleep_calls = []

    async def fake_sleep(duration):
        sleep_calls.append(duration)
        await anyio.sleep(0.001)

    client = SlackClient("bot-token", sleep=fake_sleep)
    res = await client.auth_test()
    assert res.ok is True
    assert sleep_calls == [0.01]


async def test_drop_pending_edits(mock_http_client):
    client = SlackClient("bot-token", rps=0)
    with patch.object(
        client._outbox, "drop_pending", new_callable=AsyncMock
    ) as mock_drop:
        await client.drop_pending_edits("123.456")
        mock_drop.assert_called_once_with(("edit", "123.456"))


async def test_socket_mode_events(mock_http_client):
    @asynccontextmanager
    async def fake_connect():
        yield "event_iterator"

    mock_http_client.socket_mode_connect.return_value = fake_connect()

    client = SlackClient("bot-token")
    async with client.socket_mode_events() as events:
        assert events == "event_iterator"


def test_static_loggers():
    SlackClient._log_request_error("test", Exception("err"))
    SlackClient._log_outbox_failure(Exception("err"))
