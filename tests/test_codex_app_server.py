"""Tests for the codex-app (app-server) runner.

The WebSocket/process layer is replaced with a fake server so the JSON-RPC
flow and event translation are exercised without spawning codex.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tunapi.events import EventFactory
from tunapi.model import CompletedEvent, ResumeToken, StartedEvent, ActionEvent
from tunapi.runners import codex_app_server as cas

pytestmark = pytest.mark.anyio


class FakeServer:
    def __init__(
        self,
        *,
        rpc: dict[str, Any],
        notifications: list[dict[str, Any]],
    ) -> None:
        self._rpc = rpc
        self._notes = notifications
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.notifies: list[tuple[str, dict[str, Any]]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        for note in self._notes:
            queue.put_nowait(note)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        pass

    async def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        result = self._rpc.get(method)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise AssertionError(f"unexpected rpc: {method}")
        return result

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.notifies.append((method, params))


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeServer) -> None:
    async def _fake_get(cmd: str, script: str | None) -> FakeServer:
        return fake

    monkeypatch.setattr(cas, "_get_server", _fake_get)


def _runner() -> cas.CodexAppServerRunner:
    return cas.CodexAppServerRunner(codex_cmd="codex")


async def _collect(runner: cas.CodexAppServerRunner, prompt: str, resume=None) -> list:
    return [evt async for evt in runner.run_impl(prompt, resume)]


# ───────────────────────────── backend / mixin ───────────────────────────────


def test_build_runner_returns_codex_app_engine():
    runner = cas.build_runner({}, Path("."))
    assert isinstance(runner, cas.CodexAppServerRunner)
    assert runner.engine == "codex-app"


def test_backend_id():
    assert cas.BACKEND.id == "codex-app"


def test_resume_mixin_does_not_parse_text():
    runner = _runner()
    assert runner.is_resume_line("codex resume abc") is False
    assert runner.extract_resume("codex resume abc") is None
    assert runner.format_resume(ResumeToken(engine="codex-app", value="abcdef")) == "abcde"


# ───────────────────────────── run_impl flows ────────────────────────────────


async def test_happy_path_streams_actions_and_answer(monkeypatch):
    fake = FakeServer(
        rpc={"thread/start": {"thread": {"id": "t1"}}, "turn/start": {"turn": {"id": "u1"}}},
        notifications=[
            {
                "method": "item/started",
                "params": {
                    "threadId": "t1",
                    "item": {"id": "c1", "type": "command_execution", "command": "ls"},
                },
            },
            {"method": "item/agentMessage/delta", "params": {"threadId": "t1", "delta": "hel"}},
            {"method": "item/agentMessage/delta", "params": {"threadId": "t1", "delta": "lo"}},
            {
                "method": "item/completed",
                "params": {
                    "threadId": "t1",
                    "item": {
                        "id": "c1",
                        "type": "command_execution",
                        "command": "ls",
                        "status": "completed",
                    },
                },
            },
            {"method": "turn/completed", "params": {"threadId": "t1", "turn": {"id": "u1"}}},
        ],
    )
    _install(monkeypatch, fake)
    events = await _collect(_runner(), "hi")

    assert isinstance(events[0], StartedEvent)
    assert events[0].resume == ResumeToken(engine="codex-app", value="t1")
    actions = [e for e in events if isinstance(e, ActionEvent)]
    assert [a.phase for a in actions] == ["started", "completed"]
    assert all(a.action.kind == "command" for a in actions)
    assert isinstance(events[-1], CompletedEvent)
    assert events[-1].ok is True
    assert events[-1].answer == "hello"

    # thread/start carries the headless guards.
    start_params = dict(fake.calls)["thread/start"]
    assert start_params["approvalPolicy"] == "never"
    assert start_params["sandbox"] == "danger-full-access"
    # turn/start input shape.
    turn_params = dict(fake.calls)["turn/start"]
    assert turn_params["input"] == [{"type": "text", "text": "hi"}]
    # normal completion → no interrupt.
    assert fake.notifies == []


async def test_resume_reuses_thread(monkeypatch):
    fake = FakeServer(
        rpc={"thread/resume": {"thread": {"id": "old"}}, "turn/start": {"turn": {"id": "u1"}}},
        notifications=[
            {"method": "turn/completed", "params": {"threadId": "old", "turn": {"id": "u1"}}}
        ],
    )
    _install(monkeypatch, fake)
    events = await _collect(
        _runner(), "hi", ResumeToken(engine="codex-app", value="old")
    )
    assert events[0].resume.value == "old"
    assert ("thread/resume", {"threadId": "old", "cwd": str(Path.cwd()),
            "approvalPolicy": "never", "sandbox": "danger-full-access"}) in [
        (m, p) for m, p in fake.calls
    ]


async def test_resume_falls_back_to_new_thread(monkeypatch):
    fake = FakeServer(
        rpc={
            "thread/resume": RuntimeError("unknown thread"),
            "thread/start": {"thread": {"id": "new"}},
            "turn/start": {"turn": {"id": "u1"}},
        },
        notifications=[
            {"method": "turn/completed", "params": {"threadId": "new", "turn": {"id": "u1"}}}
        ],
    )
    _install(monkeypatch, fake)
    events = await _collect(
        _runner(), "hi", ResumeToken(engine="codex-app", value="stale")
    )
    assert events[0].resume.value == "new"
    assert any(m == "thread/start" for m, _ in fake.calls)


async def test_error_without_retry_completes_error(monkeypatch):
    fake = FakeServer(
        rpc={"thread/start": {"thread": {"id": "t1"}}, "turn/start": {"turn": {"id": "u1"}}},
        notifications=[
            {
                "method": "error",
                "params": {"threadId": "t1", "error": {"message": "boom"}, "willRetry": False},
            }
        ],
    )
    _install(monkeypatch, fake)
    events = await _collect(_runner(), "hi")
    assert isinstance(events[-1], CompletedEvent)
    assert events[-1].ok is False
    assert "boom" in (events[-1].error or "")


async def test_connection_closed_completes_error(monkeypatch):
    fake = FakeServer(
        rpc={"thread/start": {"thread": {"id": "t1"}}, "turn/start": {"turn": {"id": "u1"}}},
        notifications=[None],  # sentinel = server closed
    )
    _install(monkeypatch, fake)
    events = await _collect(_runner(), "hi")
    assert isinstance(events[-1], CompletedEvent)
    assert events[-1].ok is False


# ───────────────────────────── _CodexAppServer ───────────────────────────────


_STOP = object()


class FakeWS:
    def __init__(self) -> None:
        self._q: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False

    def feed(self, msg: dict[str, Any]) -> None:
        self._q.put_nowait(json.dumps(msg))

    def stop(self) -> None:
        self._q.put_nowait(_STOP)

    def __aiter__(self) -> FakeWS:
        return self

    async def __anext__(self) -> str:
        item = await self._q.get()
        if item is _STOP:
            raise StopAsyncIteration
        return item

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class FakeProc:
    def __init__(self) -> None:
        self.killed = False

    def kill(self) -> None:
        self.killed = True


def test_find_free_port_returns_int():
    port = cas._find_free_port()
    assert isinstance(port, int) and port > 0


def test_thread_params_includes_guards_and_optional_model():
    base = cas._thread_params(None, "/tmp/x")
    assert base["approvalPolicy"] == "never"
    assert base["sandbox"] == "danger-full-access"
    assert "model" not in base
    assert cas._thread_params("o3", "/tmp/x")["model"] == "o3"


def test_item_event_maps_kinds_and_skips_unknown():
    runner = _runner()
    factory = EventFactory("codex-app")
    started = runner._item_event(
        factory,
        "item/started",
        {"item": {"id": "c1", "type": "command_execution", "command": "ls -la"}},
    )
    assert started is not None and started.action.kind == "command"
    assert started.phase == "started"
    failed = runner._item_event(
        factory,
        "item/completed",
        {"item": {"id": "c1", "type": "command_execution", "command": "x", "status": "failed"}},
    )
    assert failed is not None and failed.ok is False
    assert runner._item_event(factory, "item/started", {"item": {"type": "mystery"}}) is None
    assert runner._item_event(factory, "item/started", {"item": "notdict"}) is None


async def test_server_rpc_resolves_by_id(monkeypatch):
    ws = FakeWS()
    server = cas._CodexAppServer(FakeProc(), ws, 1234)
    try:

        async def responder() -> None:
            while not ws.sent:
                await asyncio.sleep(0)
            req = json.loads(ws.sent[-1])
            ws.feed({"id": req["id"], "result": {"ok": 1}})

        asyncio.create_task(responder())
        result = await server.rpc("thread/start", {})
        assert result == {"ok": 1}
    finally:
        await server.aclose()


async def test_server_rpc_raises_on_error(monkeypatch):
    ws = FakeWS()
    server = cas._CodexAppServer(FakeProc(), ws, 1234)
    try:

        async def responder() -> None:
            while not ws.sent:
                await asyncio.sleep(0)
            req = json.loads(ws.sent[-1])
            ws.feed({"id": req["id"], "error": {"message": "nope"}})

        asyncio.create_task(responder())
        with pytest.raises(RuntimeError, match="nope"):
            await server.rpc("x", {})
    finally:
        await server.aclose()


async def test_server_broadcasts_notifications():
    ws = FakeWS()
    server = cas._CodexAppServer(FakeProc(), ws, 1234)
    try:
        queue = server.subscribe()
        ws.feed({"method": "item/started", "params": {"threadId": "t"}})
        msg = await asyncio.wait_for(queue.get(), 1.0)
        assert msg is not None and msg["method"] == "item/started"
    finally:
        await server.aclose()


async def test_server_closed_sends_sentinel():
    ws = FakeWS()
    server = cas._CodexAppServer(FakeProc(), ws, 1234)
    queue = server.subscribe()
    ws.stop()  # ends the read loop → connection closed
    msg = await asyncio.wait_for(queue.get(), 1.0)
    assert msg is None
    assert server.closed is True


async def test_server_notify_sends_and_aclose_kills():
    ws = FakeWS()
    proc = FakeProc()
    server = cas._CodexAppServer(proc, ws, 1234)
    await server.notify("turn/interrupt", {"threadId": "t"})
    assert any("turn/interrupt" in s for s in ws.sent)
    await server.aclose()
    assert proc.killed is True
    assert server.closed is True


async def test_other_thread_notifications_ignored(monkeypatch):
    fake = FakeServer(
        rpc={"thread/start": {"thread": {"id": "t1"}}, "turn/start": {"turn": {"id": "u1"}}},
        notifications=[
            {"method": "item/agentMessage/delta", "params": {"threadId": "OTHER", "delta": "x"}},
            {"method": "item/agentMessage/delta", "params": {"threadId": "t1", "delta": "ok"}},
            {"method": "turn/completed", "params": {"threadId": "t1", "turn": {"id": "u1"}}},
        ],
    )
    _install(monkeypatch, fake)
    events = await _collect(_runner(), "hi")
    assert events[-1].answer == "ok"
