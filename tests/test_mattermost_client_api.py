"""Tests for the low-level Mattermost HTTP client."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.anyio

from tunapi.mattermost.api_models import Channel, FileInfo, Post, User
from tunapi.mattermost.client_api import (
    HttpMattermostClient,
    MattermostRetryAfter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_response(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=data,
        request=httpx.Request("POST", "http://mm.test/api/v4/test"),
    )


def _empty_response(status: int = 204) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        request=httpx.Request("DELETE", "http://mm.test/api/v4/test"),
    )


class FakeTransport(httpx.AsyncBaseTransport):
    """Records requests and returns canned responses."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []
        self._next_responses: list[httpx.Response] = []

    def enqueue(self, resp: httpx.Response) -> None:
        self._next_responses.append(resp)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._next_responses:
            resp = self._next_responses.pop(0)
            resp._request = request  # noqa: SLF001 – patch request for logging
            return resp
        return _json_response({})


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def client(transport: FakeTransport) -> HttpMattermostClient:
    http = httpx.AsyncClient(transport=transport)
    return HttpMattermostClient(
        "https://mm.test",
        "test-token",
        http_client=http,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_empty_token_raises(self):
        with pytest.raises(ValueError, match="empty"):
            HttpMattermostClient("https://mm.test", "")

    def test_auth_header_set(self, client: HttpMattermostClient):
        assert client._http_client.headers["Authorization"] == "Bearer test-token"  # noqa: SLF001


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class TestGetMe:
    async def test_success(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({"id": "bot1", "username": "tunapi-bot", "roles": "system_user"})
        )
        user = await client.get_me()
        assert isinstance(user, User)
        assert user.id == "bot1"
        assert user.username == "tunapi-bot"

    async def test_network_error(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            httpx.Response(
                status_code=502,
                text="Bad Gateway",
                request=httpx.Request("GET", "http://mm.test/api/v4/users/me"),
            )
        )
        result = await client.get_me()
        assert result is None


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------


class TestCreatePost:
    async def test_simple_post(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({"id": "p1", "channel_id": "c1", "message": "hello"})
        )
        post = await client.create_post("c1", "hello")
        assert isinstance(post, Post)
        assert post.id == "p1"
        assert post.message == "hello"

    async def test_with_root_id(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({"id": "p2", "channel_id": "c1", "root_id": "p1", "message": "reply"})
        )
        post = await client.create_post("c1", "reply", root_id="p1")
        assert isinstance(post, Post)
        assert post.root_id == "p1"

    async def test_with_props(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({"id": "p3", "channel_id": "c1", "message": "hi", "props": {"k": "v"}})
        )
        post = await client.create_post("c1", "hi", props={"k": "v"})
        assert isinstance(post, Post)
        assert post.props == {"k": "v"}


class TestUpdatePost:
    async def test_success(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({"id": "p1", "channel_id": "c1", "message": "edited"})
        )
        post = await client.update_post("p1", "edited")
        assert isinstance(post, Post)
        assert post.message == "edited"


class TestDeletePost:
    async def test_success(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(_empty_response(204))
        result = await client.delete_post("p1")
        assert result is True

    async def test_api_error(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response(
                {"id": "api.post.delete_post.error", "message": "not found"},
                status=404,
            )
        )
        result = await client.delete_post("p999")
        assert result is False


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class TestGetChannel:
    async def test_success(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({
                "id": "ch1",
                "type": "O",
                "display_name": "General",
                "name": "general",
                "team_id": "t1",
            })
        )
        ch = await client.get_channel("ch1")
        assert isinstance(ch, Channel)
        assert ch.display_name == "General"


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class TestUploadFile:
    async def test_success(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({
                "file_infos": [{
                    "id": "f1",
                    "name": "test.txt",
                    "size": 100,
                    "mime_type": "text/plain",
                    "extension": "txt",
                }]
            })
        )
        fi = await client.upload_file("c1", "test.txt", b"hello")
        assert isinstance(fi, FileInfo)
        assert fi.id == "f1"

    async def test_empty_response(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"file_infos": []}))
        fi = await client.upload_file("c1", "test.txt", b"hello")
        assert fi is None


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    async def test_429_raises_retry_after(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        resp = httpx.Response(
            status_code=429,
            json={"message": "rate limited"},
            headers={"Retry-After": "3"},
            request=httpx.Request("POST", "http://mm.test/api/v4/posts"),
        )
        transport.enqueue(resp)
        with pytest.raises(MattermostRetryAfter) as exc_info:
            await client.create_post("c1", "too fast")
        assert exc_info.value.retry_after == 3.0

    async def test_429_default_retry(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        resp = httpx.Response(
            status_code=429,
            json={"message": "rate limited"},
            request=httpx.Request("POST", "http://mm.test/api/v4/posts"),
        )
        transport.enqueue(resp)
        with pytest.raises(MattermostRetryAfter) as exc_info:
            await client.create_post("c1", "too fast")
        assert exc_info.value.retry_after == 5.0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    async def test_api_error_returns_none(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response(
                {"id": "error.id", "message": "bad request"},
                status=400,
            )
        )
        result = await client.get_me()
        assert result is None
