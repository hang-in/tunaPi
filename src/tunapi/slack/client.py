"""High-level Slack client with outbox queue and rate limiting."""

from __future__ import annotations

import itertools
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from collections.abc import Hashable

import anyio

from ..logging import get_logger
from .api_models import (
    AuthTestResponse,
    ChatPostMessageResponse,
    SlackChannel,
    SlackUser,
    SocketModeEnvelope,
)
from .client_api import HttpSlackClient, SlackRetryAfter
from .outbox import DELETE_PRIORITY, EDIT_PRIORITY, SEND_PRIORITY, SlackOutbox, OutboxOp

logger = get_logger(__name__)


class SlackClient:
    """Wraps :class:`HttpSlackClient` with an outbox queue.

    All mutating operations go through the outbox for rate-limit enforcement
    and deduplication (e.g. rapid progress edits).
    """

    def __init__(
        self,
        bot_token: str,
        app_token: str | None = None,
        *,
        timeout_s: float = 30,
        rps: float = 1.0,  # Slack chat.postMessage is Tier 2 (~1/sec)
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self._client = HttpSlackClient(bot_token, app_token, timeout_s=timeout_s)
        self._clock = clock
        self._sleep = sleep
        interval = 0.0 if rps <= 0 else 1.0 / rps
        self._outbox = SlackOutbox(
            interval=interval,
            retry_after_type=SlackRetryAfter,
            clock=clock,
            sleep=sleep,
            on_error=self._log_request_error,
            on_outbox_error=self._log_outbox_failure,
        )
        self._seq = itertools.count()

    async def close(self) -> None:
        await self._outbox.close()
        await self._client.close()

    def _unique_key(self, prefix: str) -> tuple[str, int]:
        return (prefix, next(self._seq))

    @staticmethod
    def _log_request_error(label: str, exc: Exception) -> None:
        logger.error("slack.request_error", label=label, error=str(exc))

    @staticmethod
    def _log_outbox_failure(exc: Exception) -> None:
        logger.critical("slack.outbox_failure", error=str(exc))

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
            except SlackRetryAfter as exc:
                await self._sleep(exc.retry_after)

    async def auth_test(self) -> AuthTestResponse:
        return await self._call_with_retry(self._client.auth_test)

    async def get_user(self, user_id: str) -> SlackUser | None:
        return await self._call_with_retry(lambda: self._client.get_user_info(user_id))

    async def get_channel(self, channel_id: str) -> SlackChannel | None:
        return await self._call_with_retry(
            lambda: self._client.get_channel_info(channel_id)
        )

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None = None,
    ) -> ChatPostMessageResponse | None:
        return await self._enqueue_op(
            key=self._unique_key("send"),
            label="send_message",
            execute=lambda: self._client.post_message(
                channel_id, text, thread_ts=thread_ts
            ),
            priority=SEND_PRIORITY,
        )

    async def edit_message(
        self,
        channel_id: str,
        ts: str,
        text: str,
        *,
        wait: bool = True,
    ) -> ChatPostMessageResponse | None:
        return await self._enqueue_op(
            key=("edit", ts),
            label="edit_message",
            execute=lambda: self._client.update_message(channel_id, ts, text),
            priority=EDIT_PRIORITY,
            wait=wait,
        )

    async def delete_message(self, channel_id: str, ts: str) -> bool:
        result = await self._enqueue_op(
            key=("delete", ts),
            label="delete_message",
            execute=lambda: self._client.delete_message(channel_id, ts),
            priority=DELETE_PRIORITY,
        )
        return bool(result and result.ok)

    async def add_reaction(self, channel_id: str, ts: str, name: str) -> bool:
        result = await self._enqueue_op(
            key=self._unique_key("reaction"),
            label="add_reaction",
            execute=lambda: self._client.add_reaction(channel_id, ts, name),
            priority=SEND_PRIORITY,
        )
        return bool(result and result.ok)

    async def upload_file(
        self,
        filename: str,
        content: bytes,
        *,
        channel_id: str | None = None,
        thread_ts: str | None = None,
    ) -> str | None:
        """Upload a file using the v2 flow."""

        # This is a complex multi-step op, we treat it as a single outbox op for rate limiting
        async def _upload():
            get_url_resp = await self._client.get_upload_url(filename, len(content))
            if (
                not get_url_resp.ok
                or not get_url_resp.upload_url
                or not get_url_resp.file_id
            ):
                return None

            await self._client.upload_content(get_url_resp.upload_url, content)

            complete_resp = await self._client.complete_upload_external(
                get_url_resp.file_id, channel_id=channel_id, thread_ts=thread_ts
            )
            return get_url_resp.file_id if complete_resp.ok else None

        return await self._enqueue_op(
            key=self._unique_key("upload"),
            label="upload_file",
            execute=_upload,
            priority=SEND_PRIORITY,
        )

    @asynccontextmanager
    async def socket_mode_events(
        self,
    ) -> AsyncIterator[AsyncIterator[SocketModeEnvelope]]:
        async with self._client.socket_mode_connect() as events:
            yield events

    async def drop_pending_edits(self, ts: str) -> None:
        await self._outbox.drop_pending(("edit", ts))
