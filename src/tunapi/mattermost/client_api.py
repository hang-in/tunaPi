"""Low-level Mattermost REST API v4 + WebSocket client."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import httpx
import msgspec
import websockets
import websockets.asyncio.client

from ..logging import get_logger
from .api_models import (
    Channel,
    FileInfo,
    Post,
    User,
    WebSocketAuthReply,
    WebSocketEvent,
    decode_ws_event,
)

logger = get_logger(__name__)

T = TypeVar("T")


class MattermostRetryAfter(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"retry after {retry_after}")
        self.retry_after = retry_after


class MattermostApiError(Exception):
    def __init__(
        self,
        status_code: int,
        error_id: str = "",
        message: str = "",
    ) -> None:
        super().__init__(message or f"Mattermost API error {status_code}")
        self.status_code = status_code
        self.error_id = error_id


class HttpMattermostClient:
    """Mattermost REST API + WebSocket client.

    Authentication uses ``Authorization: Bearer {token}`` for both REST and
    WebSocket connections.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_s: float = 30,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token:
            raise ValueError("Mattermost token is empty")
        self._base_url = url.rstrip("/")
        self._api = f"{self._base_url}/api/v4"
        self._token = token
        self._http_client = http_client or httpx.AsyncClient(
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {token}"},
        )
        self._owns_http_client = http_client is None
        if http_client is not None:
            self._http_client.headers.setdefault("Authorization", f"Bearer {token}")

    async def close(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_response(
        self,
        *,
        method: str,
        resp: httpx.Response,
    ) -> Any | None:
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "5"))
            logger.warning(
                "mattermost.rate_limited",
                method=method,
                url=str(resp.request.url),
                retry_after=retry_after,
            )
            raise MattermostRetryAfter(retry_after)

        if resp.status_code == 204:
            return True

        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            if resp.is_success:
                return None
            logger.error(
                "mattermost.bad_response",
                method=method,
                status=resp.status_code,
                url=str(resp.request.url),
                error=str(exc),
                body=resp.text[:500],
            )
            return None

        if not resp.is_success:
            error_id = payload.get("id", "") if isinstance(payload, dict) else ""
            error_msg = payload.get("message", "") if isinstance(payload, dict) else ""
            logger.error(
                "mattermost.api_error",
                method=method,
                status=resp.status_code,
                url=str(resp.request.url),
                error_id=error_id,
                error_msg=error_msg,
            )
            return None

        logger.debug("mattermost.response", method=method, status=resp.status_code)
        return payload

    def _decode_result(
        self,
        *,
        method: str,
        payload: Any,
        model: type[T],
    ) -> T | None:
        if payload is None:
            return None
        try:
            return msgspec.convert(payload, type=model)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "mattermost.decode_error",
                method=method,
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            return None

    async def _request(
        self,
        http_method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any | None:
        url = f"{self._api}{path}"
        label = f"{http_method} {path}"
        logger.debug("mattermost.request", method=label, json=json)
        try:
            resp = await self._http_client.request(
                http_method,
                url,
                json=json,
                data=data,
                files=files,
            )
        except httpx.HTTPError as exc:
            request_url = getattr(exc.request, "url", None)
            logger.error(
                "mattermost.network_error",
                method=label,
                url=str(request_url) if request_url is not None else None,
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            return None

        return self._parse_response(method=label, resp=resp)

    async def _get(self, path: str) -> Any | None:
        return await self._request("GET", path)

    async def _post(self, path: str, json_data: dict[str, Any]) -> Any | None:
        return await self._request("POST", path, json=json_data)

    async def _put(self, path: str, json_data: dict[str, Any]) -> Any | None:
        return await self._request("PUT", path, json=json_data)

    async def _delete(self, path: str) -> Any | None:
        return await self._request("DELETE", path)

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def get_me(self) -> User | None:
        result = await self._get("/users/me")
        return self._decode_result(method="get_me", payload=result, model=User)

    async def get_user(self, user_id: str) -> User | None:
        result = await self._get(f"/users/{user_id}")
        return self._decode_result(method="get_user", payload=result, model=User)

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    async def get_channel(self, channel_id: str) -> Channel | None:
        result = await self._get(f"/channels/{channel_id}")
        return self._decode_result(
            method="get_channel", payload=result, model=Channel
        )

    async def create_direct_channel(
        self, user_id_1: str, user_id_2: str
    ) -> Channel | None:
        result = await self._post("/channels/direct", [user_id_1, user_id_2])  # type: ignore[arg-type]
        return self._decode_result(
            method="create_direct_channel", payload=result, model=Channel
        )

    # ------------------------------------------------------------------
    # Posts
    # ------------------------------------------------------------------

    async def create_post(
        self,
        channel_id: str,
        message: str,
        *,
        root_id: str | None = None,
        file_ids: list[str] | None = None,
        props: dict[str, object] | None = None,
    ) -> Post | None:
        body: dict[str, Any] = {
            "channel_id": channel_id,
            "message": message,
        }
        if root_id:
            body["root_id"] = root_id
        if file_ids:
            body["file_ids"] = file_ids
        if props:
            body["props"] = props
        result = await self._post("/posts", body)
        return self._decode_result(method="create_post", payload=result, model=Post)

    async def get_post(self, post_id: str) -> Post | None:
        result = await self._get(f"/posts/{post_id}")
        return self._decode_result(method="get_post", payload=result, model=Post)

    async def update_post(
        self,
        post_id: str,
        message: str,
        *,
        props: dict[str, object] | None = None,
    ) -> Post | None:
        body: dict[str, Any] = {
            "id": post_id,
            "message": message,
        }
        if props is not None:
            body["props"] = props
        result = await self._put(f"/posts/{post_id}", body)
        return self._decode_result(method="update_post", payload=result, model=Post)

    async def patch_post(
        self,
        post_id: str,
        *,
        message: str | None = None,
        props: dict[str, object] | None = None,
        file_ids: list[str] | None = None,
    ) -> Post | None:
        body: dict[str, Any] = {}
        if message is not None:
            body["message"] = message
        if props is not None:
            body["props"] = props
        if file_ids is not None:
            body["file_ids"] = file_ids
        result = await self._put(f"/posts/{post_id}/patch", body)
        return self._decode_result(method="patch_post", payload=result, model=Post)

    async def delete_post(self, post_id: str) -> bool:
        result = await self._delete(f"/posts/{post_id}")
        return result is not None

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    async def upload_file(
        self,
        channel_id: str,
        filename: str,
        content: bytes,
    ) -> FileInfo | None:
        result = await self._request(
            "POST",
            "/files",
            data={"channel_id": channel_id},
            files={"files": (filename, content)},
        )
        if isinstance(result, dict):
            infos = result.get("file_infos", [])
            if infos:
                return self._decode_result(
                    method="upload_file", payload=infos[0], model=FileInfo
                )
        return None

    async def get_file(self, file_id: str) -> bytes | None:
        url = f"{self._api}/files/{file_id}"
        try:
            resp = await self._http_client.get(url)
        except httpx.HTTPError as exc:
            logger.error(
                "mattermost.file_network_error",
                url=url,
                error=str(exc),
            )
            return None
        if not resp.is_success:
            logger.error(
                "mattermost.file_http_error",
                status=resp.status_code,
                url=url,
            )
            return None
        return resp.content

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    async def add_reaction(
        self, user_id: str, post_id: str, emoji_name: str
    ) -> bool:
        result = await self._post(
            "/reactions",
            {
                "user_id": user_id,
                "post_id": post_id,
                "emoji_name": emoji_name,
            },
        )
        return result is not None

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def websocket_connect(
        self,
    ) -> AsyncIterator[AsyncIterator[WebSocketEvent]]:
        """Connect to the Mattermost WebSocket and yield an event iterator.

        Usage::

            async with client.websocket_connect() as events:
                async for event in events:
                    ...
        """
        scheme = "wss" if self._base_url.startswith("https") else "ws"
        host = self._base_url.split("://", 1)[1]
        ws_url = f"{scheme}://{host}/api/v4/websocket"
        logger.info("mattermost.ws_connecting", url=ws_url)

        auth_headers = {
            "Authorization": f"Bearer {self._token}",
            "Origin": self._base_url,
        }

        async with websockets.asyncio.client.connect(
            ws_url, additional_headers=auth_headers
        ) as ws:
            # Wait for hello event (confirms auth via Bearer header)
            raw_reply = await ws.recv()
            raw = raw_reply if isinstance(raw_reply, bytes) else raw_reply.encode()
            reply = decode_ws_event(raw)
            if isinstance(reply, WebSocketEvent) and reply.event == "hello":
                logger.info("mattermost.ws_authenticated")
            elif isinstance(reply, WebSocketAuthReply) and reply.status != "OK":
                raise MattermostApiError(
                    401,
                    error_id="ws_auth_failed",
                    message=f"WebSocket auth failed: {reply.status}",
                )
            else:
                logger.info("mattermost.ws_connected", first_event=getattr(reply, "event", "unknown"))

            async def _iter_events() -> AsyncIterator[WebSocketEvent]:
                async for frame in ws:
                    raw = frame if isinstance(frame, bytes) else frame.encode()
                    parsed = decode_ws_event(raw)
                    if isinstance(parsed, WebSocketEvent) and parsed.event:
                        yield parsed

            yield _iter_events()
