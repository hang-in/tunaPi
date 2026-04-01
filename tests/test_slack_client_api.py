"""Tests for the low-level Slack HTTP client."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.anyio

from tunapi.slack.api_models import (
    AuthTestResponse,
    ChatPostMessageResponse,
    FilesCompleteUploadExternalResponse,
    FilesGetUploadURLExternalResponse,
    ReactionsAddResponse,
    SlackChannel,
    SlackResponse,
    SlackUser,
)
from tunapi.slack.client_api import HttpSlackClient, SlackRetryAfter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_response(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=data,
        headers=headers or {},
        request=httpx.Request("POST", "https://slack.com/api/test"),
    )


class FakeTransport(httpx.AsyncBaseTransport):
    """Records requests and returns canned responses."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._next_responses: list[httpx.Response] = []

    def enqueue(self, resp: httpx.Response) -> None:
        self._next_responses.append(resp)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._next_responses:
            resp = self._next_responses.pop(0)
            resp._request = request  # noqa: SLF001
            return resp
        return _json_response({"ok": True})


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def client(transport: FakeTransport) -> HttpSlackClient:
    real_client = httpx.AsyncClient(transport=transport, base_url="https://slack.com/api/")
    c = HttpSlackClient.__new__(HttpSlackClient)
    c._bot_token = "xoxb-test-token"  # noqa: SLF001
    c._app_token = "xapp-test-token"  # noqa: SLF001
    c._base_url = "https://slack.com/api/"  # noqa: SLF001
    c._client = real_client  # noqa: SLF001
    return c


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_auth_header_set(self):
        c = HttpSlackClient("xoxb-bot", "xapp-app")
        assert c._client.headers["Authorization"] == "Bearer xoxb-bot"  # noqa: SLF001

    def test_default_base_url(self):
        c = HttpSlackClient("xoxb-bot")
        assert c._base_url == "https://slack.com/api/"  # noqa: SLF001

    def test_custom_base_url(self):
        c = HttpSlackClient("xoxb-bot", base_url="https://custom.slack/api/")
        assert c._base_url == "https://custom.slack/api/"  # noqa: SLF001

    def test_no_app_token(self):
        c = HttpSlackClient("xoxb-bot")
        assert c._app_token is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# SlackRetryAfter exception
# ---------------------------------------------------------------------------


class TestSlackRetryAfter:
    def test_attributes(self):
        exc = SlackRetryAfter(3.5)
        assert exc.retry_after == 3.5
        assert "3.5" in str(exc)


# ---------------------------------------------------------------------------
# _request internals
# ---------------------------------------------------------------------------


class TestRequest:
    async def test_429_raises_retry_after(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(
            httpx.Response(
                status_code=429,
                json={"ok": False, "error": "rate_limited"},
                headers={"Retry-After": "7"},
                request=httpx.Request("POST", "https://slack.com/api/test"),
            )
        )
        with pytest.raises(SlackRetryAfter) as exc_info:
            await client._request("POST", "test")  # noqa: SLF001
        assert exc_info.value.retry_after == 7.0

    async def test_429_default_retry_after(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(
            httpx.Response(
                status_code=429,
                json={"ok": False},
                request=httpx.Request("POST", "https://slack.com/api/test"),
            )
        )
        with pytest.raises(SlackRetryAfter) as exc_info:
            await client._request("POST", "test")  # noqa: SLF001
        assert exc_info.value.retry_after == 1.0

    async def test_http_error_raises(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(
            httpx.Response(
                status_code=500,
                json={"ok": False},
                request=httpx.Request("POST", "https://slack.com/api/test"),
            )
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client._request("POST", "test")  # noqa: SLF001

    async def test_api_error_logged_but_returned(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": False, "error": "channel_not_found"}))
        data = await client._request("POST", "test")  # noqa: SLF001
        assert data["ok"] is False
        assert data["error"] == "channel_not_found"

    async def test_use_app_token(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": True, "url": "wss://test"}))
        await client._request("POST", "apps.connections.open", use_app_token=True)  # noqa: SLF001
        req = transport.requests[-1]
        assert req.headers["Authorization"] == "Bearer xapp-test-token"

    async def test_use_app_token_missing_raises(self, transport: FakeTransport):
        real_client = httpx.AsyncClient(transport=transport, base_url="https://slack.com/api/")
        c = HttpSlackClient.__new__(HttpSlackClient)
        c._bot_token = "xoxb-test"  # noqa: SLF001
        c._app_token = None  # noqa: SLF001
        c._base_url = "https://slack.com/api/"  # noqa: SLF001
        c._client = real_client  # noqa: SLF001
        with pytest.raises(ValueError, match="App token required"):
            await c._request("POST", "test", use_app_token=True)  # noqa: SLF001

    async def test_json_data_sent(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": True}))
        await client._request("POST", "test", json_data={"key": "val"})  # noqa: SLF001
        req = transport.requests[-1]
        import json

        body = json.loads(req.content)
        assert body == {"key": "val"}

    async def test_params_sent(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": True}))
        await client._request("GET", "test", params={"user": "U123"})  # noqa: SLF001
        req = transport.requests[-1]
        assert b"user=U123" in req.url.raw_path


# ---------------------------------------------------------------------------
# auth_test
# ---------------------------------------------------------------------------


class TestAuthTest:
    async def test_success(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({
                "ok": True,
                "url": "https://test.slack.com",
                "team": "Test",
                "user": "bot",
                "team_id": "T1",
                "user_id": "U1",
                "bot_id": "B1",
            })
        )
        result = await client.auth_test()
        assert isinstance(result, AuthTestResponse)
        assert result.ok is True
        assert result.user_id == "U1"
        assert result.bot_id == "B1"


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class TestPostMessage:
    async def test_simple(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({
                "ok": True,
                "channel": "C1",
                "ts": "1234.5678",
            })
        )
        result = await client.post_message("C1", "hello")
        assert isinstance(result, ChatPostMessageResponse)
        assert result.channel == "C1"
        assert result.ts == "1234.5678"

    async def test_with_thread(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": True, "channel": "C1", "ts": "1234.9999"}))
        result = await client.post_message("C1", "reply", thread_ts="1234.0000")
        assert result.ts == "1234.9999"
        import json

        body = json.loads(transport.requests[-1].content)
        assert body["thread_ts"] == "1234.0000"

    async def test_reply_broadcast(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": True, "channel": "C1", "ts": "1"}))
        await client.post_message("C1", "loud", thread_ts="0", reply_broadcast=True)
        import json

        body = json.loads(transport.requests[-1].content)
        assert body["reply_broadcast"] is True


class TestUpdateMessage:
    async def test_success(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": True, "channel": "C1", "ts": "1234.5678"}))
        result = await client.update_message("C1", "1234.5678", "edited")
        assert isinstance(result, ChatPostMessageResponse)
        import json

        body = json.loads(transport.requests[-1].content)
        assert body["ts"] == "1234.5678"
        assert body["text"] == "edited"


class TestDeleteMessage:
    async def test_success(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": True}))
        result = await client.delete_message("C1", "1234.5678")
        assert isinstance(result, SlackResponse)
        assert result.ok is True


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


class TestAddReaction:
    async def test_success(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": True}))
        result = await client.add_reaction("C1", "1234.5678", "thumbsup")
        assert isinstance(result, ReactionsAddResponse)
        assert result.ok is True
        import json

        body = json.loads(transport.requests[-1].content)
        assert body["name"] == "thumbsup"
        assert body["channel"] == "C1"
        assert body["timestamp"] == "1234.5678"


# ---------------------------------------------------------------------------
# User / Channel info
# ---------------------------------------------------------------------------


class TestGetUserInfo:
    async def test_success(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({
                "ok": True,
                "user": {"id": "U1", "name": "alice", "real_name": "Alice", "is_bot": False},
            })
        )
        user = await client.get_user_info("U1")
        assert isinstance(user, SlackUser)
        assert user.id == "U1"
        assert user.name == "alice"

    async def test_not_found(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": False, "error": "user_not_found"}))
        user = await client.get_user_info("U999")
        assert user is None


class TestGetChannelInfo:
    async def test_success(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({
                "ok": True,
                "channel": {"id": "C1", "name": "general", "is_channel": True},
            })
        )
        ch = await client.get_channel_info("C1")
        assert isinstance(ch, SlackChannel)
        assert ch.id == "C1"
        assert ch.name == "general"

    async def test_not_found(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": False, "error": "channel_not_found"}))
        ch = await client.get_channel_info("C999")
        assert ch is None


# ---------------------------------------------------------------------------
# File uploads
# ---------------------------------------------------------------------------


class TestGetUploadUrl:
    async def test_success(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({
                "ok": True,
                "upload_url": "https://files.slack.com/upload/xxx",
                "file_id": "F1",
            })
        )
        result = await client.get_upload_url("test.txt", 100)
        assert isinstance(result, FilesGetUploadURLExternalResponse)
        assert result.upload_url == "https://files.slack.com/upload/xxx"
        assert result.file_id == "F1"

    async def test_with_alt_text(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": True, "upload_url": "https://u", "file_id": "F2"}))
        await client.get_upload_url("img.png", 200, alt_text="screenshot")
        req = transport.requests[-1]
        assert b"alt_text=screenshot" in req.url.raw_path

    async def test_without_alt_text(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": True, "upload_url": "https://u", "file_id": "F3"}))
        await client.get_upload_url("doc.txt", 50)
        req = transport.requests[-1]
        assert b"alt_text" not in req.url.raw_path


class TestCompleteUploadExternal:
    async def test_minimal(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": True, "files": []}))
        result = await client.complete_upload_external("F1")
        assert isinstance(result, FilesCompleteUploadExternalResponse)
        import json

        body = json.loads(transport.requests[-1].content)
        assert body["files"] == [{"id": "F1"}]
        assert "thread_ts" not in body
        assert "channel_id" not in body

    async def test_with_thread_and_channel(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": True, "files": []}))
        await client.complete_upload_external("F1", thread_ts="1.2", channel_id="C1")
        import json

        body = json.loads(transport.requests[-1].content)
        assert body["thread_ts"] == "1.2"
        assert body["channel_id"] == "C1"


# ---------------------------------------------------------------------------
# Socket Mode
# ---------------------------------------------------------------------------


class TestAppsConnectionsOpen:
    async def test_success(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": True, "url": "wss://test.slack.com/link"}))
        url = await client.apps_connections_open()
        assert url == "wss://test.slack.com/link"
        # Verify app token was used
        req = transport.requests[-1]
        assert req.headers["Authorization"] == "Bearer xapp-test-token"

    async def test_failure_returns_none(
        self, client: HttpSlackClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"ok": False, "error": "invalid_auth"}))
        url = await client.apps_connections_open()
        assert url is None


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    async def test_close(self, client: HttpSlackClient):
        await client.close()
        assert client._client.is_closed  # noqa: SLF001
