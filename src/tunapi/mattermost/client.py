"""High-level Mattermost client with outbox queue and rate limiting."""

from __future__ import annotations

import itertools
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from collections.abc import Hashable

import anyio

from ..logging import get_logger
from .api_models import Channel, FileInfo, Post, User, WebSocketEvent
from .client_api import HttpMattermostClient, MattermostRetryAfter
from .outbox import (
    DELETE_PRIORITY,
    EDIT_PRIORITY,
    SEND_PRIORITY,
    MattermostOutbox,
    OutboxOp,
)

logger = get_logger(__name__)


class MattermostClient:
    """Wraps :class:`HttpMattermostClient` with an outbox queue.

    All mutating operations go through the outbox for rate-limit enforcement
    and deduplication (e.g. rapid progress edits).
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_s: float = 30,
        rps: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self._client = HttpMattermostClient(url, token, timeout_s=timeout_s)
        self._clock = clock
        self._sleep = sleep
        interval = 0.0 if rps <= 0 else 1.0 / rps
        self._outbox = MattermostOutbox(
            interval=interval,
            retry_after_type=MattermostRetryAfter,
            clock=clock,
            sleep=sleep,
            on_error=self._log_request_error,
            on_outbox_error=self._log_outbox_failure,
        )
        self._seq = itertools.count()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await self._outbox.close()
        await self._client.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _unique_key(self, prefix: str) -> tuple[str, int]:
        return (prefix, next(self._seq))

    @staticmethod
    def _log_request_error(label: str, exc: Exception) -> None:
        logger.error("mattermost.request_error", label=label, error=str(exc))

    @staticmethod
    def _log_outbox_failure(exc: Exception) -> None:
        logger.critical("mattermost.outbox_failure", error=str(exc))

    async def _enqueue_op(
        self,
        *,
        key: Hashable,
        label: str,
        execute: Callable[[], Awaitable[Any]],
        priority: int,
        wait: bool = True,
    ) -> Any:
        op = OutboxOp(
            execute=execute,
            priority=priority,
            queued_at=self._clock(),
            label=label,
        )
        return await self._outbox.enqueue(key, op, wait=wait)

    async def _call_with_retry(
        self,
        fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        while True:
            try:
                return await fn()
            except MattermostRetryAfter as exc:
                await self._sleep(exc.retry_after)

    # ------------------------------------------------------------------
    # Users (read-only, bypass outbox)
    # ------------------------------------------------------------------

    async def get_me(self) -> User | None:
        return await self._call_with_retry(self._client.get_me)

    async def get_user(self, user_id: str) -> User | None:
        return await self._call_with_retry(lambda: self._client.get_user(user_id))

    # ------------------------------------------------------------------
    # Channels (read-only, bypass outbox)
    # ------------------------------------------------------------------

    async def get_channel(self, channel_id: str) -> Channel | None:
        return await self._call_with_retry(lambda: self._client.get_channel(channel_id))

    # ------------------------------------------------------------------
    # Posts (through outbox)
    # ------------------------------------------------------------------

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        root_id: str | None = None,
        props: dict[str, object] | None = None,
        file_ids: list[str] | None = None,
    ) -> Post | None:
        return await self._enqueue_op(
            key=self._unique_key("send"),
            label="send_message",
            execute=lambda: self._client.create_post(
                channel_id,
                text,
                root_id=root_id,
                file_ids=file_ids,
                props=props,
            ),
            priority=SEND_PRIORITY,
        )

    async def edit_message(
        self,
        post_id: str,
        text: str,
        *,
        props: dict[str, object] | None = None,
        wait: bool = True,
    ) -> Post | None:
        return await self._enqueue_op(
            key=("edit", post_id),
            label="edit_message",
            execute=lambda: self._client.update_post(post_id, text, props=props),
            priority=EDIT_PRIORITY,
            wait=wait,
        )

    async def delete_message(self, post_id: str) -> bool:
        result = await self._enqueue_op(
            key=("delete", post_id),
            label="delete_message",
            execute=lambda: self._client.delete_post(post_id),
            priority=DELETE_PRIORITY,
        )
        return bool(result)

    # ------------------------------------------------------------------
    # Files (through outbox)
    # ------------------------------------------------------------------

    async def upload_file(
        self,
        channel_id: str,
        filename: str,
        content: bytes,
    ) -> FileInfo | None:
        return await self._enqueue_op(
            key=self._unique_key("upload"),
            label="upload_file",
            execute=lambda: self._client.upload_file(channel_id, filename, content),
            priority=SEND_PRIORITY,
        )

    async def get_file(self, file_id: str) -> bytes | None:
        return await self._call_with_retry(lambda: self._client.get_file(file_id))

    # ------------------------------------------------------------------
    # Reactions (through outbox)
    # ------------------------------------------------------------------

    async def add_reaction(self, user_id: str, post_id: str, emoji_name: str) -> bool:
        result = await self._enqueue_op(
            key=self._unique_key("reaction"),
            label="add_reaction",
            execute=lambda: self._client.add_reaction(user_id, post_id, emoji_name),
            priority=SEND_PRIORITY,
        )
        return bool(result)

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def websocket_events(self) -> AsyncIterator[AsyncIterator[WebSocketEvent]]:
        async with self._client.websocket_connect() as events:
            yield events

    # ------------------------------------------------------------------
    # Drop helpers (for deduplication)
    # ------------------------------------------------------------------

    async def drop_pending_edits(self, post_id: str) -> None:
        await self._outbox.drop_pending(("edit", post_id))
