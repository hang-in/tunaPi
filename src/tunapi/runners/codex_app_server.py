"""Codex `app-server` persistent-session engine.

Spawns one global ``codex app-server --listen ws://127.0.0.1:<port>`` process,
shared across conversations, and drives it over a JSON-RPC/WebSocket protocol.
Unlike the per-message ``codex exec`` runner this keeps the process (and each
channel's conversation thread) warm, so follow-up turns skip the cold start.

Ported from the working tunaFlow implementation
(`src-tauri/src/agents/codex_app_server.rs`); protocol source of truth:
`codex-rs/app-server-protocol`. Engine id: ``codex_app`` — coexists with the
``codex`` exec engine so it can be opted into per channel (`!model codex_app`).
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import itertools
import json
import os
import re
import shutil
import signal
import socket
from pathlib import Path
from typing import Any

from ..backends import EngineBackend, EngineConfig
from ..config import ConfigError
from ..events import EventFactory
from ..logging import get_logger
from ..model import ActionKind, EngineId, ResumeToken, TunapiEvent
from ..runner import BaseRunner, ResumeTokenMixin, Runner
from .run_options import get_run_options

logger = get_logger(__name__)

ENGINE: EngineId = "codex_app"

# app-server never prints a resume line into agent output (the thread id is an
# internal handle), so this pattern is intentionally unmatchable.
_NO_RESUME_RE = re.compile(r"(?!)")

_RPC_TIMEOUT_S = 30.0
_TURN_TIMEOUT_S = 600.0
_WS_CONNECT_ATTEMPTS = 20
_WS_CONNECT_DELAY_S = 0.5

__all__ = ["ENGINE", "CodexAppServerRunner", "BACKEND", "build_runner"]


# ───────────────────────────── global server ─────────────────────────────────


class _CodexAppServer:
    """A live ``codex app-server`` process + its WebSocket connection.

    One instance is shared process-wide. Incoming messages are demultiplexed:
    responses (carry ``id``) resolve pending RPC futures; notifications (carry
    ``method``) are broadcast to every subscriber queue.
    """

    def __init__(self, proc: asyncio.subprocess.Process, ws: Any, port: int) -> None:
        self._proc = proc
        self._ws = ws
        self.port = port
        self.pid = proc.pid
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self._closed = False
        self._reader = asyncio.create_task(self._read_loop())

    @property
    def closed(self) -> bool:
        return self._closed

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if "method" in msg:
                    for queue in list(self._subscribers):
                        queue.put_nowait(msg)
                elif "id" in msg:
                    fut = self._pending.pop(str(msg["id"]), None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
        except Exception:  # noqa: BLE001 — connection dropped; tear down below
            logger.warning("codex_app_server.ws_closed", exc_info=True)
        finally:
            self._mark_closed()

    def _mark_closed(self) -> None:
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("codex app-server connection closed"))
        self._pending.clear()
        for queue in list(self._subscribers):
            queue.put_nowait(None)

    def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        self._subscribers.discard(queue)

    async def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("codex app-server is not connected")
        rpc_id = str(next(self._ids))
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[rpc_id] = fut
        await self._ws.send(json.dumps({"id": rpc_id, "method": method, "params": params}))
        try:
            msg = await asyncio.wait_for(fut, _RPC_TIMEOUT_S)
        finally:
            self._pending.pop(rpc_id, None)
        if "error" in msg:
            raise RuntimeError(f"codex app-server RPC error ({method}): {msg['error']}")
        result = msg.get("result")
        return result if isinstance(result, dict) else {}

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if self._closed:
            return
        with contextlib.suppress(Exception):
            await self._ws.send(
                json.dumps({"id": str(next(self._ids)), "method": method, "params": params})
            )

    async def aclose(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(Exception):
            await self._ws.close()
        self.terminate()

    def terminate(self) -> None:
        """Synchronously SIGKILL the spawned process group by pgid. Safe to call
        at interpreter exit (no event loop needed). Targets ONLY our own group
        (the launcher + the worker it spawned) — never pattern-match, which
        would hit the user's Codex desktop app."""
        self._closed = True
        # The process was started with start_new_session=True, so pgid == pid.
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(self.pid, signal.SIGKILL)


_server: _CodexAppServer | None = None
_server_lock = asyncio.Lock()
_atexit_registered = False


def _atexit_kill() -> None:  # pragma: no cover - runs at interpreter exit
    """Kill the spawned app-server so it does not orphan when tunapi exits.

    Covers graceful/SIGTERM shutdown (atexit runs); a hard SIGKILL of tunapi
    cannot run this — accepted edge case.
    """
    if _server is not None:
        _server.terminate()


def _ensure_atexit() -> None:
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(_atexit_kill)
        _atexit_registered = True


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _connect_ws(url: str) -> Any:  # pragma: no cover - needs a live ws server
    # Imported lazily so the module loads even if websockets is unavailable.
    from websockets.asyncio.client import connect

    last_exc: Exception | None = None
    for _ in range(_WS_CONNECT_ATTEMPTS):
        await asyncio.sleep(_WS_CONNECT_DELAY_S)
        try:
            return await connect(url)
        except Exception as exc:  # noqa: BLE001 — codex not bound yet; retry
            last_exc = exc
    raise RuntimeError(f"codex app-server WS connect failed: {last_exc}")


async def _start_server(  # pragma: no cover - spawns the real codex binary
    codex_cmd: str, codex_script: str | None
) -> _CodexAppServer:
    port = _find_free_port()
    url = f"ws://127.0.0.1:{port}"
    args = [codex_cmd]
    if codex_script:
        args.append(codex_script)
    args += ["app-server", "--listen", url]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        # Own session/group: the `codex` launcher spawns the real binary as a
        # child, so we must kill the whole group (killpg) — killing just the
        # launcher pid would orphan the worker.
        start_new_session=True,
    )
    try:
        ws = await _connect_ws(url)
    except Exception:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    server = _CodexAppServer(proc, ws, port)
    # app-server requires an initialize handshake before any thread ops
    # (otherwise thread/start fails with -32600 "Not initialized").
    await server.rpc(
        "initialize",
        {
            "clientInfo": {"name": "tunapi", "version": "1"},
            "capabilities": {"experimentalApi": False},
        },
    )
    _ensure_atexit()
    logger.info("codex_app_server.started", port=port)
    return server


async def _get_server(  # pragma: no cover - process/ws lifecycle glue
    codex_cmd: str, codex_script: str | None
) -> _CodexAppServer:
    global _server
    async with _server_lock:
        if _server is not None and not _server.closed:
            return _server
        _server = await _start_server(codex_cmd, codex_script)
        return _server


# ───────────────────────────── runner ────────────────────────────────────────

_ITEM_KIND: dict[str, ActionKind] = {
    "command_execution": "command",
    "file_change": "file_change",
    "reasoning": "note",
    "web_search": "web_search",
    "mcp_tool_call": "tool",
}


def _thread_params(model: str | None, cwd: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "cwd": cwd,
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
    }
    if model:
        params["model"] = model
    return params


class CodexAppServerRunner(ResumeTokenMixin, BaseRunner):
    engine: EngineId = ENGINE
    resume_re = _NO_RESUME_RE
    logger = logger

    def __init__(self, *, codex_cmd: str, codex_script: str | None = None) -> None:
        self.codex_cmd = codex_cmd
        self.codex_script = codex_script

    async def _resolve_thread(
        self,
        server: _CodexAppServer,
        resume: ResumeToken | None,
        params: dict[str, Any],
    ) -> str:
        if resume is not None:
            try:
                result = await server.rpc(
                    "thread/resume", {"threadId": resume.value, **params}
                )
                thread = result.get("thread")
                if isinstance(thread, dict) and isinstance(thread.get("id"), str):
                    return thread["id"]
            except Exception:  # noqa: BLE001 — stale thread (e.g. after restart)
                logger.info("codex_app_server.resume_failed", thread=resume.value)
        result = await server.rpc("thread/start", params)
        thread = result.get("thread")
        if not (isinstance(thread, dict) and isinstance(thread.get("id"), str)):
            raise RuntimeError("codex app-server: thread/start returned no thread id")
        return thread["id"]

    async def run_impl(
        self, prompt: str, resume: ResumeToken | None
    ) -> Any:
        factory = EventFactory(ENGINE)
        run_options = get_run_options()
        model = run_options.model if run_options else None
        params = _thread_params(model, str(Path.cwd()))

        server = await _get_server(self.codex_cmd, self.codex_script)
        queue = server.subscribe()
        thread_id: str | None = None
        finished = False
        try:
            thread_id = await self._resolve_thread(server, resume, params)
            yield factory.started(ResumeToken(engine=ENGINE, value=thread_id))

            turn = await server.rpc(
                "turn/start",
                {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]},
            )
            turn_obj = turn.get("turn")
            turn_id = turn_obj.get("id") if isinstance(turn_obj, dict) else None

            async for event in self._stream_turn(
                queue, factory, thread_id=thread_id, turn_id=turn_id
            ):
                yield event
            finished = True
        finally:
            server.unsubscribe(queue)
            # Only interrupt if we were cancelled mid-turn (consumer stopped
            # iterating); a normally-completed turn needs no interrupt.
            if not finished and thread_id is not None and not server.closed:
                with contextlib.suppress(Exception):
                    await server.notify("turn/interrupt", {"threadId": thread_id})

    async def _stream_turn(
        self,
        queue: asyncio.Queue[dict[str, Any] | None],
        factory: EventFactory,
        *,
        thread_id: str,
        turn_id: Any,
    ) -> Any:
        accumulated: list[str] = []
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), _TURN_TIMEOUT_S)
            except TimeoutError:
                yield factory.completed_error(
                    error="codex app-server: timed out waiting for response",
                    answer="".join(accumulated),
                )
                return
            if msg is None:
                yield factory.completed_error(
                    error="codex app-server: connection closed",
                    answer="".join(accumulated),
                )
                return

            params = msg.get("params")
            params = params if isinstance(params, dict) else {}
            if params.get("threadId") not in (None, thread_id):
                continue
            method = msg.get("method") or ""

            if method == "item/agentMessage/delta":
                delta = params.get("delta")
                if isinstance(delta, str):
                    accumulated.append(delta)
            elif method in ("item/started", "item/completed"):
                event = self._item_event(factory, method, params)
                if event is not None:
                    yield event
            elif method == "turn/completed":
                turn_obj = params.get("turn")
                done_id = turn_obj.get("id") if isinstance(turn_obj, dict) else None
                if turn_id is not None and done_id != turn_id:
                    continue
                yield factory.completed_ok(answer="".join(accumulated).strip())
                return
            elif method == "error":
                err = params.get("error")
                message = err.get("message") if isinstance(err, dict) else None
                if not params.get("willRetry", False):
                    yield factory.completed_error(
                        error=str(message or "codex app-server error"),
                        answer="".join(accumulated).strip(),
                    )
                    return

    def _item_event(
        self, factory: EventFactory, method: str, params: dict[str, Any]
    ) -> TunapiEvent | None:
        item = params.get("item")
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        kind = _ITEM_KIND.get(item_type) if isinstance(item_type, str) else None
        if kind is None:
            return None
        action_id = str(item.get("id") or f"{item_type}:{id(item)}")
        title = (
            item.get("command")
            or item.get("file")
            or item.get("query")
            or item_type
        )
        title = str(title)[:120]
        if method == "item/started":
            return factory.action_started(action_id=action_id, kind=kind, title=title)
        ok = item.get("status") != "failed"
        return factory.action_completed(
            action_id=action_id, kind=kind, title=title, ok=ok
        )


# ───────────────────────────── backend ───────────────────────────────────────


def build_runner(config: EngineConfig, config_path: Path) -> Runner:
    if os.name == "nt":
        npm_root = Path.home() / "AppData" / "Roaming" / "npm"
        entry = npm_root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if entry.exists():
            codex_cmd = shutil.which("node") or "node"
            codex_script: str | None = str(entry)
        else:
            codex_cmd = shutil.which("codex") or "codex"
            codex_script = None
    else:
        codex_cmd = shutil.which("codex") or "codex"
        codex_script = None

    extra = config.get("extra_args")
    if extra is not None and not (
        isinstance(extra, list) and all(isinstance(item, str) for item in extra)
    ):
        raise ConfigError(
            f"Invalid `codex_app.extra_args` in {config_path}; expected a list of strings."
        )
    return CodexAppServerRunner(codex_cmd=codex_cmd, codex_script=codex_script)


BACKEND = EngineBackend(
    id="codex_app",
    build_runner=build_runner,
    # Availability check resolves cli_cmd (not the id) on PATH; the binary is
    # `codex`, not `codex_app`.
    cli_cmd="codex",
    install_cmd="npm install -g @openai/codex",
)
