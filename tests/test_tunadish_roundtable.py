"""Tests for tunadish roundtable wiring (tunapi.tunadish.roundtable + commands)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tunapi.context import RunContext
from tunapi.core.roundtable import RoundtableSession, RoundtableStore
from tunapi.core.roundtable.session import RoundtableBridgeCfg
from tunapi.runner_bridge import ExecBridgeConfig
from tunapi.transport import MessageRef, RenderedMessage
from tunapi.transport_runtime import RoundtableConfig
from tunapi.tunadish import roundtable as rt_mod
from tunapi.tunadish.commands import dispatch_command, handle_rt

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, RenderedMessage]] = []

    async def send(self, *, channel_id, message, options=None) -> MessageRef:
        self.sent.append((channel_id, message))
        return MessageRef(channel_id=channel_id, message_id="hdr")

    def texts(self) -> list[str]:
        return [m.text for _, m in self.sent]


def _make_runtime(
    engines: tuple[str, ...] = ("claude", "codex"),
    *,
    rounds: int = 1,
    roles: tuple[str, ...] = (),
) -> MagicMock:
    rt = MagicMock()
    rt.roundtable = RoundtableConfig(
        engines=engines,
        roles=roles,
        rounds=rounds,
        max_rounds=3,
        parallel_first_round=False,
    )
    rt.available_engine_ids.return_value = list(engines)
    return rt


def _make_context_store(project: str | None = "p1") -> MagicMock:
    store = MagicMock()
    store.get_context = AsyncMock(return_value=RunContext(project=project, branch=None))
    return store


# ---------------------------------------------------------------------------
# render_roundtable_header
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TunadishRoundtableCfg — satisfies RoundtableBridgeCfg protocol
# ---------------------------------------------------------------------------


def test_cfg_satisfies_bridge_protocol():
    runtime = _make_runtime()
    exec_cfg = ExecBridgeConfig(
        transport=FakeTransport(), presenter=MagicMock(), final_notify=False
    )
    cfg = rt_mod.TunadishRoundtableCfg(runtime=runtime, exec_cfg=exec_cfg)
    assert isinstance(cfg, RoundtableBridgeCfg)
    assert cfg.runtime is runtime
    assert cfg.exec_cfg is exec_cfg


def test_render_header_includes_topic_and_engines():
    text = rt_mod.render_roundtable_header("API design", 2, ["claude", "codex"])
    assert "**Topic:** API design" in text
    assert "`claude`" in text and "`codex`" in text
    assert "2 rounds" in text


def test_render_header_singular_round():
    text = rt_mod.render_roundtable_header("t", 1, ["claude"])
    assert "1 round" in text and "1 rounds" not in text


# ---------------------------------------------------------------------------
# dispatch_rt — start
# ---------------------------------------------------------------------------


class TestStart:
    async def test_creates_session_runs_and_completes(self, monkeypatch):
        run_rt = AsyncMock()
        monkeypatch.setattr(rt_mod, "run_roundtable", run_rt)

        transport = FakeTransport()
        store = RoundtableStore(persist_path=None)
        runtime = _make_runtime()
        ctx = _make_context_store()

        await rt_mod.dispatch_rt(
            '"topic"',
            conv_id="c1",
            runtime=runtime,
            transport=transport,
            presenter=MagicMock(),
            roundtables=store,
            running_tasks={},
            context_store=ctx,
            task_group=None,  # inline
        )

        # header sent
        assert any("**Roundtable**" in t for t in transport.texts())
        # run_roundtable invoked once with our session + cfg
        run_rt.assert_awaited_once()
        kwargs = run_rt.await_args.kwargs
        session = run_rt.await_args.args[0]
        assert session.thread_id == "c1"
        assert session.channel_id == "c1"
        assert session.topic == "topic"
        assert session.engines == ["claude", "codex"]
        assert kwargs["cfg"].runtime is runtime
        assert kwargs["cfg"].exec_cfg.transport is transport
        assert kwargs["ambient_context"].project == "p1"
        # session is completed (finally) and persisted as completed
        assert store.get_completed("c1") is not None

    async def test_spawns_on_task_group_when_provided(self, monkeypatch):
        run_rt = AsyncMock()
        monkeypatch.setattr(rt_mod, "run_roundtable", run_rt)

        transport = FakeTransport()
        store = RoundtableStore(persist_path=None)
        spawned: list = []
        tg = MagicMock()
        tg.start_soon = lambda fn, *a: spawned.append((fn, a))

        await rt_mod.dispatch_rt(
            '"topic"',
            conv_id="c1",
            runtime=_make_runtime(),
            transport=transport,
            presenter=MagicMock(),
            roundtables=store,
            running_tasks={},
            context_store=_make_context_store(),
            task_group=tg,
        )

        # background spawn: run_roundtable not awaited synchronously, session active
        assert spawned, "expected background spawn via task_group"
        run_rt.assert_not_awaited()
        assert store.get("c1") is not None
        assert store.get_completed("c1") is None

    async def test_usage_when_no_topic(self, monkeypatch):
        run_rt = AsyncMock()
        monkeypatch.setattr(rt_mod, "run_roundtable", run_rt)
        transport = FakeTransport()
        store = RoundtableStore(persist_path=None)

        await rt_mod.dispatch_rt(
            "",
            conv_id="c1",
            runtime=_make_runtime(),
            transport=transport,
            presenter=MagicMock(),
            roundtables=store,
            running_tasks={},
            context_store=_make_context_store(),
            task_group=None,
        )

        run_rt.assert_not_awaited()
        assert store.get("c1") is None
        assert any("Usage" in t for t in transport.texts())


# ---------------------------------------------------------------------------
# dispatch_rt — follow
# ---------------------------------------------------------------------------


class TestFollow:
    async def test_followup_runs_on_completed_session(self, monkeypatch):
        run_follow = AsyncMock()
        monkeypatch.setattr(rt_mod, "run_followup_round", run_follow)

        transport = FakeTransport()
        store = RoundtableStore(persist_path=None)
        session = RoundtableSession(
            thread_id="c1",
            channel_id="c1",
            topic="orig",
            engines=["claude", "codex"],
            total_rounds=1,
        )
        store.put(session)
        store.complete("c1")

        await rt_mod.dispatch_rt(
            'follow "more please"',
            conv_id="c1",
            runtime=_make_runtime(),
            transport=transport,
            presenter=MagicMock(),
            roundtables=store,
            running_tasks={},
            context_store=_make_context_store(),
            task_group=None,
        )

        run_follow.assert_awaited_once()
        args = run_follow.await_args.args
        assert args[1] == "more please"  # topic
        assert run_follow.await_args.kwargs["ambient_context"].project == "p1"

    async def test_followup_without_session_shows_hint(self, monkeypatch):
        run_follow = AsyncMock()
        monkeypatch.setattr(rt_mod, "run_followup_round", run_follow)
        transport = FakeTransport()
        store = RoundtableStore(persist_path=None)

        await rt_mod.dispatch_rt(
            'follow "q"',
            conv_id="c1",
            runtime=_make_runtime(),
            transport=transport,
            presenter=MagicMock(),
            roundtables=store,
            running_tasks={},
            context_store=_make_context_store(),
            task_group=None,
        )

        run_follow.assert_not_awaited()
        assert any("completed roundtable" in t for t in transport.texts())


# ---------------------------------------------------------------------------
# dispatch_rt — close
# ---------------------------------------------------------------------------


class TestClose:
    async def test_close_archives_and_removes(self):
        transport = FakeTransport()
        store = RoundtableStore(persist_path=None)
        session = RoundtableSession(
            thread_id="c1",
            channel_id="c1",
            topic="orig",
            engines=["claude"],
            total_rounds=1,
            transcript=[("claude", "an opinion")],
        )
        store.put(session)
        store.complete("c1")

        facade = MagicMock()
        facade.save_roundtable = AsyncMock()

        await rt_mod.dispatch_rt(
            "close",
            conv_id="c1",
            runtime=_make_runtime(),
            transport=transport,
            presenter=MagicMock(),
            roundtables=store,
            running_tasks={},
            context_store=_make_context_store(project="p1"),
            facade=facade,
            task_group=None,
        )

        assert store.get("c1") is None
        assert any("Roundtable closed." in t for t in transport.texts())
        facade.save_roundtable.assert_awaited_once()

    async def test_close_without_session_shows_hint(self):
        transport = FakeTransport()
        store = RoundtableStore(persist_path=None)

        await rt_mod.dispatch_rt(
            "close",
            conv_id="c1",
            runtime=_make_runtime(),
            transport=transport,
            presenter=MagicMock(),
            roundtables=store,
            running_tasks={},
            context_store=_make_context_store(),
            task_group=None,
        )

        assert any("roundtable thread" in t for t in transport.texts())


# ---------------------------------------------------------------------------
# commands.handle_rt + dispatch_command plumbing
# ---------------------------------------------------------------------------


class TestCommandPlumbing:
    async def test_handle_rt_fallback_without_transport(self):
        send = AsyncMock()
        await handle_rt("topic", runtime=_make_runtime(), send=send)
        text = send.call_args_list[0].args[0].text
        assert "Roundtable" in text and "`claude`" in text

    async def test_dispatch_command_rt_wires_start(self, monkeypatch):
        run_rt = AsyncMock()
        monkeypatch.setattr(rt_mod, "run_roundtable", run_rt)

        transport = FakeTransport()
        store = RoundtableStore(persist_path=None)
        send = AsyncMock()

        handled = await dispatch_command(
            "rt",
            '"topic"',
            channel_id="c1",
            runtime=_make_runtime(),
            chat_prefs=None,
            facade=None,
            journal=None,
            context_store=_make_context_store(),
            conv_sessions=None,
            running_tasks={},
            transport=transport,
            presenter=MagicMock(),
            roundtables=store,
            task_group=None,
            send=send,
        )

        assert handled is True
        run_rt.assert_awaited_once()
        assert store.get_completed("c1") is not None
