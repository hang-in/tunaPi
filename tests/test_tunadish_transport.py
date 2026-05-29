"""Tests for TunadishTransport and TunadishPresenter."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tunapi.transport import MessageRef, RenderedMessage, SendOptions
from tunapi.progress import ActionState, ProgressState
from tunapi.model import Action
from tunapi.tunadish.transport import (
    BroadcastTransport,
    TunadishTransport,
    _dc_to_dict,
)
from tunapi.tunadish.presenter import TunadishPresenter

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeWs:
    """In-memory WebSocket that records sent payloads."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def last(self) -> dict[str, Any]:
        return self.sent[-1]


def _make_action_state(
    title: str = "Read file",
    completed: bool = False,
    kind: str = "tool",
) -> ActionState:
    return ActionState(
        action=Action(id="a1", kind=kind, title=title),
        phase="started",
        ok=None,
        display_phase="started",
        completed=completed,
        first_seen=0,
        last_update=0,
    )


def _make_progress(
    actions: tuple[ActionState, ...] = (),
    engine: str = "claude",
) -> ProgressState:
    return ProgressState(
        engine=engine,
        action_count=len(actions),
        actions=actions,
        resume=None,
        resume_line=None,
        context_line=None,
    )


# ===================================================================
# _dc_to_dict
# ===================================================================


class TestDcToDict:
    def test_excludes_none_fields(self):
        ref = MessageRef(channel_id="ch1", message_id="m1")
        d = _dc_to_dict(ref)
        assert d["channel_id"] == "ch1"
        assert d["message_id"] == "m1"
        assert "raw" not in d
        assert "thread_id" not in d

    def test_includes_non_none_fields(self):
        ref = MessageRef(channel_id="ch1", message_id="m1", thread_id="t1")
        d = _dc_to_dict(ref)
        assert d["thread_id"] == "t1"


# ===================================================================
# TunadishTransport.send
# ===================================================================


class TestSend:
    async def test_send_returns_message_ref(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        msg = RenderedMessage(text="hello")
        ref = await t.send(channel_id="ch1", message=msg)

        assert ref is not None
        assert ref.channel_id == "ch1"
        assert ref.message_id  # uuid generated

    async def test_send_emits_message_new_notification(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        msg = RenderedMessage(text="hello")
        await t.send(channel_id="ch1", message=msg)

        payload = ws.last()
        assert payload["method"] == "message.new"
        assert payload["params"]["message"]["text"] == "hello"
        assert "ref" in payload["params"]

    async def test_send_includes_engine_model_meta(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        t.set_run_meta(engine="claude", model="opus-4")
        msg = RenderedMessage(text="hi")
        await t.send(channel_id="ch1", message=msg)

        params = ws.last()["params"]
        assert params["engine"] == "claude"
        assert params["model"] == "opus-4"

    async def test_send_message_extra_overrides_transport_meta(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        t.set_run_meta(engine="claude", model="opus-4")
        msg = RenderedMessage(
            text="hi", extra={"engine": "gemini", "persona": "reviewer"}
        )
        await t.send(channel_id="ch1", message=msg)

        params = ws.last()["params"]
        assert params["engine"] == "gemini"
        assert params["model"] == "opus-4"  # not overridden
        assert params["persona"] == "reviewer"

    async def test_send_with_options_still_works(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        msg = RenderedMessage(text="hi")
        opts = SendOptions(notify=False)
        ref = await t.send(channel_id="ch1", message=msg, options=opts)
        assert ref is not None
        assert len(ws.sent) == 1

    async def test_send_with_rpc_id_becomes_response(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        t.set_rpc_id("rpc-1")
        msg = RenderedMessage(text="hi")
        await t.send(channel_id="ch1", message=msg)

        payload = ws.last()
        assert payload["jsonrpc"] == "2.0"
        assert payload["id"] == "rpc-1"
        assert "method" not in payload


# ===================================================================
# TunadishTransport.edit
# ===================================================================


class TestEdit:
    async def test_edit_emits_message_update(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        ref = MessageRef(channel_id="ch1", message_id="m1")
        msg = RenderedMessage(text="updated")
        result = await t.edit(ref=ref, message=msg)

        assert result is ref
        payload = ws.last()
        assert payload["method"] == "message.update"
        assert payload["params"]["message"]["text"] == "updated"

    async def test_edit_includes_meta(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        t.set_run_meta(engine="codex", model="o3")
        ref = MessageRef(channel_id="ch1", message_id="m1")
        msg = RenderedMessage(text="progress")
        await t.edit(ref=ref, message=msg)

        params = ws.last()["params"]
        assert params["engine"] == "codex"
        assert params["model"] == "o3"


# ===================================================================
# TunadishTransport.delete
# ===================================================================


class TestDelete:
    async def test_delete_emits_message_delete(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        ref = MessageRef(channel_id="ch1", message_id="m1")
        ok = await t.delete(ref=ref)

        assert ok is True
        payload = ws.last()
        assert payload["method"] == "message.delete"
        assert payload["params"]["ref"]["channel_id"] == "ch1"

    async def test_delete_no_meta(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        t.set_run_meta(engine="claude", model="opus")
        ref = MessageRef(channel_id="ch1", message_id="m1")
        await t.delete(ref=ref)

        params = ws.last()["params"]
        # delete should not include engine/model
        assert "engine" not in params
        assert "model" not in params


# ===================================================================
# TunadishTransport — closed state
# ===================================================================


class TestClosedBehavior:
    async def test_send_noop_when_closed(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        t._closed = True

        ref = await t.send(channel_id="ch1", message=RenderedMessage(text="x"))
        assert ref is None or len(ws.sent) == 0
        assert len(ws.sent) == 0

    async def test_edit_noop_when_closed(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        t._closed = True

        ref = MessageRef(channel_id="ch1", message_id="m1")
        await t.edit(ref=ref, message=RenderedMessage(text="x"))
        assert len(ws.sent) == 0

    async def test_delete_noop_when_closed(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        t._closed = True

        ref = MessageRef(channel_id="ch1", message_id="m1")
        await t.delete(ref=ref)
        assert len(ws.sent) == 0

    async def test_ws_error_marks_closed(self):
        ws = AsyncMock()
        ws.send.side_effect = ConnectionError("gone")
        t = TunadishTransport(ws)

        assert t._closed is False
        await t.send(channel_id="ch1", message=RenderedMessage(text="hi"))
        assert t._closed is True

    async def test_send_response_noop_when_closed(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        t._closed = True
        await t._send_response("req-1", {"ok": True})
        assert len(ws.sent) == 0

    async def test_send_error_noop_when_closed(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        t._closed = True
        await t._send_error("req-1", -32000, "fail")
        assert len(ws.sent) == 0

    async def test_send_response_ws_error_marks_closed(self):
        ws = AsyncMock()
        ws.send.side_effect = RuntimeError("disconnect")
        t = TunadishTransport(ws)
        await t._send_response("req-1", {"ok": True})
        assert t._closed is True

    async def test_send_error_ws_error_marks_closed(self):
        ws = AsyncMock()
        ws.send.side_effect = RuntimeError("disconnect")
        t = TunadishTransport(ws)
        await t._send_error("req-1", -32000, "fail")
        assert t._closed is True


# ===================================================================
# TunadishTransport._build_meta
# ===================================================================


class TestBuildMeta:
    def test_empty_when_no_meta_set(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        msg = RenderedMessage(text="x")
        assert t._build_meta(msg) == {}

    def test_transport_level_meta(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        t.set_run_meta(engine="claude", model="sonnet")
        msg = RenderedMessage(text="x")
        meta = t._build_meta(msg)
        assert meta == {"engine": "claude", "model": "sonnet"}

    def test_extra_overrides_engine(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        t.set_run_meta(engine="claude", model="sonnet")
        msg = RenderedMessage(text="x", extra={"engine": "gemini"})
        meta = t._build_meta(msg)
        assert meta["engine"] == "gemini"
        assert meta["model"] == "sonnet"

    def test_extra_adds_persona(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        msg = RenderedMessage(text="x", extra={"persona": "critic"})
        meta = t._build_meta(msg)
        assert meta == {"persona": "critic"}

    def test_extra_none_values_ignored(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        t.set_run_meta(engine="claude", model="sonnet")
        msg = RenderedMessage(text="x", extra={"engine": None})
        meta = t._build_meta(msg)
        assert meta["engine"] == "claude"  # not overridden


# ===================================================================
# TunadishTransport.close
# ===================================================================


class TestClose:
    async def test_close_is_noop(self):
        ws = FakeWs()
        t = TunadishTransport(ws)
        await t.close()  # should not raise
        assert len(ws.sent) == 0


# ===================================================================
# TunadishPresenter.render_progress
# ===================================================================


class TestRenderProgress:
    def test_basic_label(self):
        p = TunadishPresenter()
        state = _make_progress()
        result = p.render_progress(state, elapsed_s=3.5, label="thinking")
        assert "**thinking** (3.5s)" in result.text

    def test_default_label(self):
        p = TunadishPresenter()
        state = _make_progress()
        result = p.render_progress(state, elapsed_s=1.0)
        assert "**working** (1.0s)" in result.text

    def test_empty_label_no_header(self):
        p = TunadishPresenter()
        state = _make_progress()
        result = p.render_progress(state, elapsed_s=1.0, label="")
        # With no label and no actions, falls back to placeholder
        assert "진행 중" in result.text

    def test_actions_displayed(self):
        p = TunadishPresenter()
        actions = (
            _make_action_state("Read file", completed=True),
            _make_action_state("Write output", completed=False),
        )
        state = _make_progress(actions=actions)
        result = p.render_progress(state, elapsed_s=5.0)
        assert "✅ Read file" in result.text
        assert "⏳ Write output" in result.text

    def test_no_label_no_actions_fallback(self):
        p = TunadishPresenter()
        state = _make_progress()
        result = p.render_progress(state, elapsed_s=0.0, label="")
        assert result.text == "⏳ 진행 중..."

    def test_returns_rendered_message(self):
        p = TunadishPresenter()
        state = _make_progress()
        result = p.render_progress(state, elapsed_s=1.0)
        assert isinstance(result, RenderedMessage)


# ===================================================================
# TunadishPresenter.render_final
# ===================================================================


class TestRenderFinal:
    def test_error_status(self):
        p = TunadishPresenter()
        state = _make_progress()
        result = p.render_final(state, elapsed_s=1.0, status="error", answer="oops")
        assert "오류" in result.text

    def test_cancelled_status(self):
        p = TunadishPresenter()
        state = _make_progress()
        result = p.render_final(state, elapsed_s=1.0, status="cancelled", answer="")
        assert "취소" in result.text

    def test_success_with_answer(self):
        p = TunadishPresenter()
        state = _make_progress()
        result = p.render_final(state, elapsed_s=2.0, status="ok", answer="Done!")
        assert result.text == "Done!"

    def test_success_empty_answer(self):
        p = TunadishPresenter()
        state = _make_progress()
        result = p.render_final(state, elapsed_s=2.0, status="ok", answer="")
        assert "응답 없음" in result.text

    def test_returns_rendered_message(self):
        p = TunadishPresenter()
        state = _make_progress()
        result = p.render_final(state, elapsed_s=1.0, status="ok", answer="hi")
        assert isinstance(result, RenderedMessage)


# ---------------------------------------------------------------------------
# BroadcastTransport
# ---------------------------------------------------------------------------


class _FakeBackend:
    def __init__(self, transports) -> None:
        self._active_transports = set(transports)


class TestBroadcastTransport:
    async def test_send_fans_out_to_all_windows_with_one_ref(self):
        ws1, ws2 = FakeWs(), FakeWs()
        t1, t2 = TunadishTransport(ws1), TunadishTransport(ws2)
        bt = BroadcastTransport(_FakeBackend([t1, t2]), primary=t1)

        ref = await bt.send(channel_id="c1", message=RenderedMessage(text="hi"))

        # both windows received the same message.new with the same ref
        assert ws1.last()["method"] == "message.new"
        assert ws2.last()["method"] == "message.new"
        assert ws1.last()["params"]["ref"] == ws2.last()["params"]["ref"]
        assert ref is not None
        assert ws1.last()["params"]["ref"]["message_id"] == ref.message_id

    async def test_primary_included_even_if_not_active(self):
        ws_primary = FakeWs()
        primary = TunadishTransport(ws_primary)
        # primary NOT in the active set (reconnect race / unit test)
        bt = BroadcastTransport(_FakeBackend([]), primary=primary)

        await bt.send(channel_id="c1", message=RenderedMessage(text="hi"))
        assert len(ws_primary.sent) == 1

    async def test_no_duplicate_when_primary_also_active(self):
        ws = FakeWs()
        primary = TunadishTransport(ws)
        bt = BroadcastTransport(_FakeBackend([primary]), primary=primary)

        await bt.send(channel_id="c1", message=RenderedMessage(text="hi"))
        assert len(ws.sent) == 1  # deduped, not 2

    async def test_meta_included_in_broadcast(self):
        ws = FakeWs()
        bt = BroadcastTransport(_FakeBackend([]), primary=TunadishTransport(ws))
        bt.set_run_meta("claude", "claude-opus-4-8")

        await bt.send(channel_id="c1", message=RenderedMessage(text="hi"))
        params = ws.last()["params"]
        assert params["engine"] == "claude"
        assert params["model"] == "claude-opus-4-8"

    async def test_edit_targets_same_ref_on_all(self):
        ws1, ws2 = FakeWs(), FakeWs()
        t1, t2 = TunadishTransport(ws1), TunadishTransport(ws2)
        bt = BroadcastTransport(_FakeBackend([t1, t2]), primary=t1)

        ref = MessageRef(channel_id="c1", message_id="m1")
        await bt.edit(ref=ref, message=RenderedMessage(text="edited"))

        assert ws1.last()["method"] == "message.update"
        assert ws2.last()["method"] == "message.update"
        assert ws2.last()["params"]["ref"]["message_id"] == "m1"

    async def test_broadcast_not_hijacked_by_pending_rpc_id(self):
        # A window mid-RPC has a pending rpc_id; a run broadcast must NOT be
        # converted into that window's RPC response (notify bypasses set_rpc_id).
        ws = FakeWs()
        other = TunadishTransport(ws)
        other.set_rpc_id(42)
        bt = BroadcastTransport(_FakeBackend([other]), primary=other)

        await bt.send(channel_id="c1", message=RenderedMessage(text="hi"))
        assert ws.last()["method"] == "message.new"  # not a {"id": 42} response
        assert "id" not in ws.last()
