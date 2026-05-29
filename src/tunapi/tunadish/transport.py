from typing import Any, Protocol

from ..transport import (
    ChannelId,
    MessageRef,
    RenderedMessage,
    SendOptions,
    Transport,
)

import dataclasses
import json
import uuid

from ..logging import get_logger

logger = get_logger(__name__)


class WsSendable(Protocol):
    async def send(self, data: str) -> None: ...


def _dc_to_dict(obj: Any) -> dict[str, Any]:
    """dataclass를 dict로 변환, None 필드 제외"""
    return {k: v for k, v in dataclasses.asdict(obj).items() if v is not None}


def build_meta(
    run_engine: str | None,
    run_model: str | None,
    message: RenderedMessage,
) -> dict[str, Any]:
    """engine/model/persona metadata for a client notification.

    ``message.extra`` overrides the run-level engine/model.
    """
    meta: dict[str, Any] = {}
    if run_engine:
        meta["engine"] = run_engine
    if run_model:
        meta["model"] = run_model
    if message.extra:
        for key in ("engine", "model", "persona"):
            val = message.extra.get(key)
            if val is not None:
                meta[key] = val
    return meta


class TunadishTransport(Transport):
    """
    tunadish 클라이언트로 rendered message를 전파(Relay)하는 Transport 구현체.
    """

    def __init__(self, ws: WsSendable):
        self._ws = ws
        self._pending_rpc_id: str | int | None = None
        self._run_engine: str | None = None
        self._run_model: str | None = None
        self._closed: bool = False

    def set_run_meta(self, engine: str | None, model: str | None) -> None:
        """Set engine/model for the current run (included in message notifications)."""
        self._run_engine = engine
        self._run_model = model

    def set_rpc_id(self, rpc_id: str | int | None) -> None:
        """다음 _send_notification 호출을 JSON-RPC 2.0 response로 변환."""
        self._pending_rpc_id = rpc_id

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        if self._closed:
            return
        rpc_id = self._pending_rpc_id
        if rpc_id is not None:
            self._pending_rpc_id = None
            if "error" in params and isinstance(params["error"], str):
                raw = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {"code": -32000, "message": params["error"]},
                    }
                )
            else:
                raw = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": params})
        else:
            raw = json.dumps({"method": method, "params": params})
        try:
            await self._ws.send(raw)
        except Exception as e:  # noqa: BLE001
            self._closed = True
            logger.warning("ws.push_failed (marking closed)", error=str(e))

    async def _send_response(self, rpc_id: str | int, result: dict[str, Any]) -> None:
        """JSON-RPC 2.0 성공 response 전송."""
        if self._closed:
            return
        raw = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result})
        try:
            await self._ws.send(raw)
        except Exception as e:  # noqa: BLE001
            self._closed = True
            logger.warning("ws.push_failed (marking closed)", error=str(e))

    async def _send_error(self, rpc_id: str | int, code: int, message: str) -> None:
        """JSON-RPC 2.0 에러 response 전송."""
        if self._closed:
            return
        raw = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": code, "message": message},
            }
        )
        try:
            await self._ws.send(raw)
        except Exception as e:  # noqa: BLE001
            self._closed = True
            logger.warning("ws.push_failed (marking closed)", error=str(e))

    def _build_meta(self, message: RenderedMessage) -> dict[str, Any]:
        """Build engine/model/persona metadata for client notifications."""
        return build_meta(self._run_engine, self._run_model, message)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a plain notification that is never converted to an RPC response.

        Used by BroadcastTransport: fanning a message out to other windows must
        not get hijacked by some unrelated pending rpc_id on those connections.
        """
        if self._closed:
            return
        try:
            await self._ws.send(json.dumps({"method": method, "params": params}))
        except Exception as e:  # noqa: BLE001
            self._closed = True
            logger.warning("ws.push_failed (marking closed)", error=str(e))

    async def send(
        self,
        *,
        channel_id: ChannelId,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef | None:
        ref = MessageRef(channel_id=channel_id, message_id=str(uuid.uuid4()))
        params: dict[str, Any] = {
            "ref": _dc_to_dict(ref),
            "message": _dc_to_dict(message),
        }
        params.update(self._build_meta(message))
        await self._send_notification(method="message.new", params=params)
        return ref

    async def edit(
        self,
        *,
        ref: MessageRef,
        message: RenderedMessage,
        wait: bool = True,
    ) -> MessageRef | None:
        params: dict[str, Any] = {
            "ref": _dc_to_dict(ref),
            "message": _dc_to_dict(message),
        }
        params.update(self._build_meta(message))
        await self._send_notification(method="message.update", params=params)
        return ref

    async def delete(self, *, ref: MessageRef) -> bool:
        await self._send_notification(
            method="message.delete",
            params={"ref": _dc_to_dict(ref)},
        )
        return True

    async def close(self) -> None:
        pass


class BroadcastTransport(Transport):
    """Fans a run's output out to every connected tunadish window.

    A single ``MessageRef`` is minted per send so ``edit`` targets the same
    message on every window. The ``primary`` (the connection that started the
    run) is always included, even if it is not yet in ``_active_transports``
    (tests, races). Delivery uses ``notify`` so a broadcast is never hijacked by
    an unrelated pending rpc_id on another window.

    Caveat: this syncs only *currently* connected windows — a client that
    reconnects mid-run does not get a replay of earlier messages (it is told
    ``run.status: running`` on connect; full replay is future work).
    """

    def __init__(self, backend: Any, primary: TunadishTransport):
        self._backend = backend
        self._primary = primary
        self._run_engine: str | None = None
        self._run_model: str | None = None

    def set_run_meta(self, engine: str | None, model: str | None) -> None:
        self._run_engine = engine
        self._run_model = model

    def _targets(self) -> list[TunadishTransport]:
        targets = [self._primary]
        seen = {id(self._primary)}
        for t in self._backend._active_transports:
            if id(t) not in seen:
                seen.add(id(t))
                targets.append(t)
        return targets

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        for t in self._targets():
            await t.notify(method, params)

    async def send(
        self,
        *,
        channel_id: ChannelId,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef | None:
        ref = MessageRef(channel_id=channel_id, message_id=str(uuid.uuid4()))
        params: dict[str, Any] = {
            "ref": _dc_to_dict(ref),
            "message": _dc_to_dict(message),
        }
        params.update(build_meta(self._run_engine, self._run_model, message))
        await self._send_notification("message.new", params)
        return ref

    async def edit(
        self,
        *,
        ref: MessageRef,
        message: RenderedMessage,
        wait: bool = True,
    ) -> MessageRef | None:
        params: dict[str, Any] = {
            "ref": _dc_to_dict(ref),
            "message": _dc_to_dict(message),
        }
        params.update(build_meta(self._run_engine, self._run_model, message))
        await self._send_notification("message.update", params)
        return ref

    async def delete(self, *, ref: MessageRef) -> bool:
        await self._send_notification("message.delete", {"ref": _dc_to_dict(ref)})
        return True

    async def close(self) -> None:
        pass
