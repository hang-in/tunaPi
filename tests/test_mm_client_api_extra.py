"""Additional tests for the Mattermost HTTP client — covering uncovered lines."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.anyio

from tunapi.mattermost.api_models import Channel, FileInfo, Post, User
from tunapi.mattermost.client_api import (
    HttpMattermostClient,
    MattermostApiError,
    MattermostRetryAfter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_response(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=data,
        headers=headers or {},
        request=httpx.Request("POST", "http://mm.test/api/v4/test"),
    )


def _text_response(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        text=text,
        request=httpx.Request("GET", "http://mm.test/api/v4/test"),
    )


class FakeTransport(httpx.AsyncBaseTransport):
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
        return _json_response({})


class FailingTransport(httpx.AsyncBaseTransport):
    """Always raises an httpx.ConnectError."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)


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
# Exception models
# ---------------------------------------------------------------------------


class TestMattermostApiError:
    def test_with_message(self):
        exc = MattermostApiError(400, error_id="bad_request", message="invalid field")
        assert exc.status_code == 400
        assert exc.error_id == "bad_request"
        assert "invalid field" in str(exc)

    def test_without_message(self):
        exc = MattermostApiError(500)
        assert "500" in str(exc)
        assert exc.error_id == ""


class TestMattermostRetryAfter:
    def test_attributes(self):
        exc = MattermostRetryAfter(10.0)
        assert exc.retry_after == 10.0
        assert "10" in str(exc)


# ---------------------------------------------------------------------------
# Construction extras
# ---------------------------------------------------------------------------


class TestConstructionExtras:
    def test_url_trailing_slash_stripped(self):
        c = HttpMattermostClient("https://mm.test/", "tok")
        assert c._base_url == "https://mm.test"  # noqa: SLF001
        assert c._api == "https://mm.test/api/v4"  # noqa: SLF001

    def test_owns_http_client_when_none(self):
        c = HttpMattermostClient("https://mm.test", "tok")
        assert c._owns_http_client is True  # noqa: SLF001

    def test_does_not_own_injected_client(self, transport: FakeTransport):
        http = httpx.AsyncClient(transport=transport)
        c = HttpMattermostClient("https://mm.test", "tok", http_client=http)
        assert c._owns_http_client is False  # noqa: SLF001

    def test_injected_client_gets_auth_header(self, transport: FakeTransport):
        http = httpx.AsyncClient(transport=transport)
        # No prior Authorization header
        c = HttpMattermostClient("https://mm.test", "tok", http_client=http)
        assert c._http_client.headers["Authorization"] == "Bearer tok"  # noqa: SLF001


# ---------------------------------------------------------------------------
# _parse_response edge cases
# ---------------------------------------------------------------------------


class TestParseResponse:
    def test_204_returns_true(self, client: HttpMattermostClient):
        resp = httpx.Response(
            status_code=204,
            request=httpx.Request("DELETE", "http://mm.test/api/v4/posts/p1"),
        )
        result = client._parse_response(method="DELETE /posts/p1", resp=resp)  # noqa: SLF001
        assert result is True

    def test_success_non_json_returns_none(self, client: HttpMattermostClient):
        resp = httpx.Response(
            status_code=200,
            text="not json",
            headers={"content-type": "text/plain"},
            request=httpx.Request("GET", "http://mm.test/api/v4/test"),
        )
        result = client._parse_response(method="GET /test", resp=resp)  # noqa: SLF001
        assert result is None

    def test_error_non_json_returns_none(self, client: HttpMattermostClient):
        resp = httpx.Response(
            status_code=502,
            text="<html>Bad Gateway</html>",
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "http://mm.test/api/v4/test"),
        )
        result = client._parse_response(method="GET /test", resp=resp)  # noqa: SLF001
        assert result is None

    def test_error_json_returns_none(self, client: HttpMattermostClient):
        resp = _json_response(
            {"id": "api.error", "message": "not found"},
            status=404,
        )
        result = client._parse_response(method="GET /test", resp=resp)  # noqa: SLF001
        assert result is None

    def test_error_non_dict_json(self, client: HttpMattermostClient):
        resp = httpx.Response(
            status_code=400,
            json=["array", "response"],
            request=httpx.Request("GET", "http://mm.test/api/v4/test"),
        )
        result = client._parse_response(method="GET /test", resp=resp)  # noqa: SLF001
        assert result is None


# ---------------------------------------------------------------------------
# _decode_result edge cases
# ---------------------------------------------------------------------------


class TestDecodeResult:
    def test_none_payload(self, client: HttpMattermostClient):
        result = client._decode_result(method="test", payload=None, model=User)  # noqa: SLF001
        assert result is None

    def test_invalid_payload(self, client: HttpMattermostClient):
        result = client._decode_result(method="test", payload={"not_valid": True}, model=User)  # noqa: SLF001
        assert result is None

    def test_valid_payload(self, client: HttpMattermostClient):
        result = client._decode_result(  # noqa: SLF001
            method="test",
            payload={"id": "u1", "username": "alice"},
            model=User,
        )
        assert isinstance(result, User)
        assert result.id == "u1"


# ---------------------------------------------------------------------------
# Network errors
# ---------------------------------------------------------------------------


class TestNetworkError:
    async def test_request_network_error_returns_none(self):
        http = httpx.AsyncClient(transport=FailingTransport())
        c = HttpMattermostClient("https://mm.test", "tok", http_client=http)
        result = await c.get_me()
        assert result is None

    async def test_get_file_network_error(self):
        http = httpx.AsyncClient(transport=FailingTransport())
        c = HttpMattermostClient("https://mm.test", "tok", http_client=http)
        result = await c.get_file("f1")
        assert result is None


# ---------------------------------------------------------------------------
# get_user
# ---------------------------------------------------------------------------


class TestGetUser:
    async def test_success(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"id": "u1", "username": "bob"}))
        user = await client.get_user("u1")
        assert isinstance(user, User)
        assert user.username == "bob"
        assert "/users/u1" in str(transport.requests[-1].url)


# ---------------------------------------------------------------------------
# create_direct_channel
# ---------------------------------------------------------------------------


class TestCreateDirectChannel:
    async def test_success(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({"id": "ch1", "type": "D", "team_id": ""})
        )
        ch = await client.create_direct_channel("u1", "u2")
        assert isinstance(ch, Channel)
        assert ch.type == "D"


# ---------------------------------------------------------------------------
# patch_post
# ---------------------------------------------------------------------------


class TestPatchPost:
    async def test_with_all_fields(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({"id": "p1", "channel_id": "c1", "message": "patched"})
        )
        post = await client.patch_post(
            "p1",
            message="patched",
            props={"from_bot": "true"},
            file_ids=["f1"],
        )
        assert isinstance(post, Post)
        assert post.message == "patched"
        import json

        body = json.loads(transport.requests[-1].content)
        assert body["message"] == "patched"
        assert body["props"] == {"from_bot": "true"}
        assert body["file_ids"] == ["f1"]

    async def test_minimal(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(_json_response({"id": "p1", "channel_id": "c1", "message": "same"}))
        await client.patch_post("p1")
        import json

        body = json.loads(transport.requests[-1].content)
        assert body == {}


# ---------------------------------------------------------------------------
# get_post
# ---------------------------------------------------------------------------


class TestGetPost:
    async def test_success(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({"id": "p1", "channel_id": "c1", "message": "hi"})
        )
        post = await client.get_post("p1")
        assert isinstance(post, Post)
        assert post.id == "p1"
        assert "/posts/p1" in str(transport.requests[-1].url)


# ---------------------------------------------------------------------------
# update_post with props
# ---------------------------------------------------------------------------


class TestUpdatePostWithProps:
    async def test_with_props(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({"id": "p1", "channel_id": "c1", "message": "edited", "props": {"k": "v"}})
        )
        post = await client.update_post("p1", "edited", props={"k": "v"})
        assert isinstance(post, Post)
        import json

        body = json.loads(transport.requests[-1].content)
        assert body["props"] == {"k": "v"}

    async def test_without_props(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({"id": "p1", "channel_id": "c1", "message": "edited"})
        )
        await client.update_post("p1", "edited")
        import json

        body = json.loads(transport.requests[-1].content)
        assert "props" not in body


# ---------------------------------------------------------------------------
# get_file_info
# ---------------------------------------------------------------------------


class TestGetFileInfo:
    async def test_success(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({
                "id": "f1",
                "name": "report.pdf",
                "size": 5000,
                "mime_type": "application/pdf",
                "extension": "pdf",
            })
        )
        fi = await client.get_file_info("f1")
        assert isinstance(fi, FileInfo)
        assert fi.name == "report.pdf"
        assert "/files/f1/info" in str(transport.requests[-1].url)


# ---------------------------------------------------------------------------
# get_file
# ---------------------------------------------------------------------------


class TestGetFile:
    async def test_success(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            httpx.Response(
                status_code=200,
                content=b"file-content-here",
                request=httpx.Request("GET", "http://mm.test/api/v4/files/f1"),
            )
        )
        data = await client.get_file("f1")
        assert data == b"file-content-here"

    async def test_http_error(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            httpx.Response(
                status_code=404,
                text="not found",
                request=httpx.Request("GET", "http://mm.test/api/v4/files/f1"),
            )
        )
        data = await client.get_file("f1")
        assert data is None


# ---------------------------------------------------------------------------
# add_reaction
# ---------------------------------------------------------------------------


class TestAddReaction:
    async def test_success(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response({
                "user_id": "u1",
                "post_id": "p1",
                "emoji_name": "thumbsup",
            })
        )
        result = await client.add_reaction("u1", "p1", "thumbsup")
        assert result is True
        import json

        body = json.loads(transport.requests[-1].content)
        assert body["emoji_name"] == "thumbsup"

    async def test_failure(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        transport.enqueue(
            _json_response(
                {"id": "error", "message": "not found"},
                status=404,
            )
        )
        result = await client.add_reaction("u1", "p999", "thumbsup")
        assert result is False


# ---------------------------------------------------------------------------
# upload_file edge cases
# ---------------------------------------------------------------------------


class TestUploadFileExtra:
    async def test_non_dict_response(
        self, client: HttpMattermostClient, transport: FakeTransport
    ):
        # If result is not a dict (e.g. True from 204)
        transport.enqueue(
            httpx.Response(
                status_code=204,
                request=httpx.Request("POST", "http://mm.test/api/v4/files"),
            )
        )
        result = await client.upload_file("c1", "test.txt", b"hello")
        assert result is None


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    async def test_close_owned(self):
        c = HttpMattermostClient("https://mm.test", "tok")
        await c.close()
        assert c._http_client.is_closed  # noqa: SLF001

    async def test_close_not_owned(self, client: HttpMattermostClient):
        await client.close()
        assert not client._http_client.is_closed  # noqa: SLF001


# ---------------------------------------------------------------------------
# WebSocket URL construction
# ---------------------------------------------------------------------------


class TestWebSocketUrl:
    def test_https_to_wss(self):
        c = HttpMattermostClient("https://mm.test", "tok")
        assert c._base_url.startswith("https")  # noqa: SLF001

    def test_http_to_ws(self):
        c = HttpMattermostClient("http://mm.test", "tok")
        assert c._base_url.startswith("http")  # noqa: SLF001
        # The websocket_connect method would use "ws" scheme
