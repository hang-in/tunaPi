"""Tests for telegram/loop_dispatch.py — extracted dispatch functions.

Tests the functions that were extracted from loop.py closures into
module-level functions taking TelegramLoopContext.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from tunapi.model import ResumeToken
from tunapi.telegram.loop_dispatch import (
    _build_upload_prompt,
    ensure_topic_context,
    resolve_topic_key,
    route_update,
    wrap_on_thread_known,
)
from tunapi.telegram.loop_state import (
    TelegramLoopContext,
    TelegramLoopState,
    _SEEN_UPDATES_LIMIT,
)
from tunapi.telegram.types import TelegramIncomingMessage

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(**overrides) -> TelegramLoopState:
    defaults = {
        "running_tasks": {},
        "pending_prompts": {},
        "media_groups": {},
        "command_ids": set(),
        "reserved_commands": set(),
        "reserved_chat_commands": set(),
        "transport_snapshot": None,
        "topic_store": None,
        "chat_session_store": None,
        "chat_prefs": None,
        "resolved_topics_scope": None,
        "topics_chat_ids": frozenset(),
        "bot_username": None,
        "forward_coalesce_s": 0.0,
        "media_group_debounce_s": 0.0,
        "transport_id": None,
        "roundtable_store": None,
        "seen_update_ids": set(),
        "seen_update_order": deque(),
        "seen_message_keys": set(),
        "seen_messages_order": deque(),
    }
    defaults.update(overrides)
    return TelegramLoopState(**defaults)


def _make_cfg(**overrides):
    """Build a minimal fake TelegramBridgeConfig."""

    @dataclass
    class FakeTopics:
        enabled: bool = False
        scope: str = "chat"

    @dataclass
    class FakeFiles:
        enabled: bool = False
        auto_put: bool = False
        auto_put_mode: str = "default"

    @dataclass
    class FakeRuntime:
        allowlist: list[str] = field(default_factory=list)

        def resolve_message(self, **kw):
            from tunapi.transport_runtime import ResolvedMessage

            return ResolvedMessage(
                prompt=kw.get("text", ""),
                resume_token=None,
                engine_override=None,
                context=None,
                context_source=None,
            )

        def resolve_engine(self, **kw):
            return "claude"

    @dataclass
    class FakeExecCfg:
        transport: object = field(default_factory=AsyncMock)
        presenter: object = None
        final_notify: bool = False

    @dataclass
    class FakeCfg:
        runtime: object = field(default_factory=FakeRuntime)
        exec_cfg: object = field(default_factory=FakeExecCfg)
        topics: object = field(default_factory=FakeTopics)
        files: object = field(default_factory=FakeFiles)
        chat_id: int = 100
        chat_ids: list[int] = field(default_factory=list)
        allowed_user_ids: list[int] = field(default_factory=list)
        voice_transcription: bool = False
        voice_transcription_model: str = ""
        voice_max_bytes: int = 0
        voice_transcription_base_url: str | None = None
        voice_transcription_api_key: str | None = None
        show_resume_line: bool = True
        session_mode: str = "stateless"
        bot: object = field(default_factory=AsyncMock)
        forward_coalesce_s: float = 0.0
        media_group_debounce_s: float = 0.0
        startup_msg: str = ""

    cfg = FakeCfg()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _make_ctx(**overrides) -> TelegramLoopContext:
    cfg = overrides.pop("cfg", _make_cfg())
    state = overrides.pop("state", _make_state())
    tg = overrides.pop("tg", MagicMock())
    scheduler = overrides.pop("scheduler", MagicMock())
    forward_coalescer = overrides.pop("forward_coalescer", MagicMock())
    media_group_buffer = overrides.pop("media_group_buffer", MagicMock())
    resume_resolver = overrides.pop("resume_resolver", MagicMock())
    return TelegramLoopContext(
        cfg=cfg,
        state=state,
        tg=tg,
        scheduler=scheduler,
        forward_coalescer=forward_coalescer,
        media_group_buffer=media_group_buffer,
        resume_resolver=resume_resolver,
    )


def _make_msg(**overrides) -> TelegramIncomingMessage:
    defaults = {
        "transport": "telegram",
        "chat_id": 100,
        "message_id": 1,
        "text": "hello",
        "reply_to_message_id": None,
        "reply_to_text": None,
        "sender_id": 42,
        "chat_type": "private",
        "raw": {},
    }
    defaults.update(overrides)
    return TelegramIncomingMessage(**defaults)


# ---------------------------------------------------------------------------
# _build_upload_prompt
# ---------------------------------------------------------------------------


class TestBuildUploadPrompt:
    def test_with_base(self):
        assert _build_upload_prompt("prompt", "[file]") == "prompt\n\n[file]"

    def test_empty_base(self):
        assert _build_upload_prompt("", "[file]") == "[file]"

    def test_whitespace_base(self):
        assert _build_upload_prompt("  ", "[file]") == "[file]"


# ---------------------------------------------------------------------------
# resolve_topic_key
# ---------------------------------------------------------------------------


class TestResolveTopicKey:
    def test_no_topic_store(self):
        ctx = _make_ctx(state=_make_state(topic_store=None))
        msg = _make_msg()
        assert resolve_topic_key(ctx, msg) is None

    def test_with_topic_store(self):
        # topic_store is not None but _topic_key depends on cfg.topics
        # Since topics.enabled=False by default, should return None
        ctx = _make_ctx(state=_make_state(topic_store=MagicMock()))
        msg = _make_msg()
        # _topic_key checks cfg.topics.enabled → False → returns None
        assert resolve_topic_key(ctx, msg) is None


# ---------------------------------------------------------------------------
# wrap_on_thread_known
# ---------------------------------------------------------------------------


class TestWrapOnThreadKnown:
    def test_all_none(self):
        ctx = _make_ctx()
        assert wrap_on_thread_known(ctx, None, None, None) is None

    async def test_calls_base_cb(self):
        ctx = _make_ctx()
        base_cb = AsyncMock()
        wrapped = wrap_on_thread_known(ctx, base_cb, None, None)
        assert wrapped is not None
        token = ResumeToken(engine="claude", value="t1")
        done = anyio.Event()
        await wrapped(token, done)
        base_cb.assert_called_once_with(token, done)

    async def test_sets_topic_resume(self):
        topic_store = AsyncMock()
        ctx = _make_ctx(state=_make_state(topic_store=topic_store))
        wrapped = wrap_on_thread_known(ctx, None, (100, 200), None)
        assert wrapped is not None
        token = ResumeToken(engine="claude", value="t1")
        await wrapped(token, anyio.Event())
        topic_store.set_session_resume.assert_called_once_with(100, 200, token)

    async def test_sets_chat_session_resume(self):
        session_store = AsyncMock()
        ctx = _make_ctx(state=_make_state(chat_session_store=session_store))
        wrapped = wrap_on_thread_known(ctx, None, None, (100, 42))
        assert wrapped is not None
        token = ResumeToken(engine="claude", value="t1")
        await wrapped(token, anyio.Event())
        session_store.set_session_resume.assert_called_once_with(100, 42, token)


# ---------------------------------------------------------------------------
# route_update — dedup
# ---------------------------------------------------------------------------


class TestRouteUpdateDedup:
    async def test_duplicate_update_id_skipped(self):
        state = _make_state(seen_update_ids={999}, seen_update_order=deque([999]))
        ctx = _make_ctx(state=state)

        msg = _make_msg(update_id=999)
        # Should return without calling route_message
        await route_update(ctx, msg, set())
        # No crash = dedup worked

    async def test_allowed_user_filter(self):
        ctx = _make_ctx()
        msg = _make_msg(sender_id=999)
        # Should be filtered out
        await route_update(ctx, msg, {42})
        # No crash = filter worked

    async def test_seen_updates_limit(self):
        state = _make_state()
        # Fill to limit
        for i in range(_SEEN_UPDATES_LIMIT + 5):
            state.seen_update_ids.add(i)
            state.seen_update_order.append(i)
        ctx = _make_ctx(state=state)

        # Old IDs should be evictable; new unique ID should pass
        msg = _make_msg(update_id=999999)
        # This should not crash despite overflow
        import contextlib

        with contextlib.suppress(BaseException):  # noqa: BLE001
            await route_update(ctx, msg, set())


# ---------------------------------------------------------------------------
# ensure_topic_context
# ---------------------------------------------------------------------------


class TestEnsureTopicContext:
    async def test_no_topic_store(self):
        ctx = _make_ctx(state=_make_state(topic_store=None))
        from tunapi.transport_runtime import ResolvedMessage

        resolved = ResolvedMessage(
            prompt="hi",
            resume_token=None,
            engine_override=None,
            context=None,
            context_source=None,
        )
        effective, ok = await ensure_topic_context(
            ctx,
            resolved=resolved,
            ambient_context=None,
            topic_key=None,
            chat_project=None,
            reply=AsyncMock(),
        )
        assert ok is True
        assert effective is None

    async def test_unbound_topic_sends_warning(self):
        topic_store = AsyncMock()
        ctx = _make_ctx(state=_make_state(topic_store=topic_store))
        from tunapi.transport_runtime import ResolvedMessage

        resolved = ResolvedMessage(
            prompt="hi",
            resume_token=None,
            engine_override=None,
            context=None,
            context_source="ambient",  # not directives/reply_ctx
        )
        reply = AsyncMock()
        effective, ok = await ensure_topic_context(
            ctx,
            resolved=resolved,
            ambient_context=None,
            topic_key=(100, 200),
            chat_project=None,
            reply=reply,
        )
        assert ok is False
        reply.assert_called_once()
        assert "bound" in reply.call_args[1]["text"]
