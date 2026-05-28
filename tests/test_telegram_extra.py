"""Tests for Telegram update_routing and commands/topics."""
# ruff: noqa: E402

from __future__ import annotations

from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from tunapi.telegram.api_models import User
from tunapi.telegram.onboarding import (
    check_setup as tg_check_setup,
    get_bot_info,
    wait_for_chat,
    validate_topics_onboarding,
)

from tunapi.config import ProjectConfig, ProjectsConfig
from tunapi.runners.mock import Return, ScriptRunner
from tunapi.settings import TelegramTopicsSettings
from tunapi.telegram.bridge import CANCEL_CALLBACK_DATA
from tunapi.core.chat_prefs import ChatPrefsStore
from tunapi.core.chat_sessions import ChatSessionStore

from tunapi.telegram.commands.topics import (
    _handle_chat_ctx_command,
    _handle_chat_new_command,
    _handle_ctx_command,
    _handle_new_command,
    _handle_topic_command,
    _parse_chat_ctx_args,
)
from tunapi.telegram.loop_state import TelegramLoopState
from tunapi.telegram.topic_state import TopicStateStore
from tunapi.telegram.types import (
    TelegramCallbackQuery,
    TelegramIncomingMessage,
)
from tunapi.telegram.loop_state import (
    classify_message as _classify_message,
)
from tunapi.telegram.update_routing import (
    TelegramUpdateRouter,
)
from tunapi.transport_runtime import TransportRuntime
from tests.telegram_fakes import (
    DEFAULT_ENGINE_ID,
    FakeBot,
    FakeTransport,
    _make_router,
    make_cfg,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(
    text: str = "hello",
    *,
    chat_id: int = 123,
    message_id: int = 1,
    sender_id: int | None = 1,
    thread_id: int | None = None,
    chat_type: str | None = "private",
    update_id: int | None = None,
    raw: dict[str, Any] | None = None,
    document: Any = None,
    voice: Any = None,
    media_group_id: str | None = None,
) -> TelegramIncomingMessage:
    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=sender_id,
        thread_id=thread_id,
        chat_type=chat_type,
        update_id=update_id,
        raw=raw,
        document=document,
        voice=voice,
        media_group_id=media_group_id,
    )


def _callback(
    *,
    chat_id: int = 123,
    message_id: int = 1,
    data: str | None = CANCEL_CALLBACK_DATA,
    sender_id: int | None = 1,
    update_id: int | None = None,
) -> TelegramCallbackQuery:
    return TelegramCallbackQuery(
        transport="telegram",
        chat_id=chat_id,
        message_id=message_id,
        callback_query_id="cq-1",
        data=data,
        sender_id=sender_id,
        update_id=update_id,
    )


def _make_loop_state() -> TelegramLoopState:
    return TelegramLoopState(
        running_tasks={},
        pending_prompts={},
        media_groups={},
        command_ids=set(),
        reserved_commands=set(),
        reserved_chat_commands=set(),
        transport_snapshot=None,
        topic_store=None,
        chat_session_store=None,
        chat_prefs=None,
        resolved_topics_scope=None,
        topics_chat_ids=frozenset(),
        bot_username=None,
        forward_coalesce_s=0.0,
        media_group_debounce_s=0.0,
        transport_id=None,
        roundtable_store=None,
        seen_update_ids=set(),
        seen_update_order=deque(),
        seen_message_keys=set(),
        seen_messages_order=deque(),
    )


def _runtime(tmp_path: Path) -> tuple[TransportRuntime, Path]:
    runner = ScriptRunner([Return(answer="ok")], engine=DEFAULT_ENGINE_ID)
    projects = ProjectsConfig(
        projects={
            "alpha": ProjectConfig(
                alias="Alpha",
                path=tmp_path,
                worktrees_dir=Path(".worktrees"),
            )
        },
        default_project="alpha",
    )
    state_path = tmp_path / "tunapi.toml"
    runtime = TransportRuntime(
        router=_make_router(runner),
        projects=projects,
        config_path=state_path,
    )
    return runtime, state_path


# ===========================================================================
# update_routing: _classify_message
# ===========================================================================


class TestClassifyMessage:
    def test_plain_text(self) -> None:
        msg = _msg("hello world")
        c = _classify_message(msg, files_enabled=False)
        assert c.text == "hello world"
        assert c.command_id is None
        assert c.is_cancel is False
        assert c.is_forward_candidate is False
        assert c.is_media_group_document is False

    def test_cancel_command(self) -> None:
        msg = _msg("/cancel")
        c = _classify_message(msg, files_enabled=False)
        assert c.is_cancel is True

    def test_cancel_with_bot(self) -> None:
        msg = _msg("/cancel@mybot")
        c = _classify_message(msg, files_enabled=False)
        assert c.is_cancel is True

    def test_slash_command(self) -> None:
        msg = _msg("/help")
        c = _classify_message(msg, files_enabled=False)
        assert c.command_id == "help"

    def test_forwarded_message(self) -> None:
        msg = _msg("forwarded text", raw={"forward_date": 12345})
        c = _classify_message(msg, files_enabled=False)
        assert c.is_forward_candidate is True

    def test_forwarded_with_document_not_forward_candidate(self) -> None:
        from tunapi.telegram.types import TelegramDocument

        doc = TelegramDocument(
            file_id="f1",
            file_name="test.txt",
            mime_type="text/plain",
            file_size=100,
            raw={},
        )
        msg = _msg(
            "forwarded text",
            raw={"forward_date": 12345},
            document=doc,
        )
        c = _classify_message(msg, files_enabled=False)
        assert c.is_forward_candidate is False

    def test_media_group_document(self) -> None:
        from tunapi.telegram.types import TelegramDocument

        doc = TelegramDocument(
            file_id="f1",
            file_name="test.txt",
            mime_type="text/plain",
            file_size=100,
            raw={},
        )
        msg = _msg("text", document=doc, media_group_id="mg1")
        c = _classify_message(msg, files_enabled=True)
        assert c.is_media_group_document is True

    def test_media_group_document_files_disabled(self) -> None:
        from tunapi.telegram.types import TelegramDocument

        doc = TelegramDocument(
            file_id="f1",
            file_name="test.txt",
            mime_type="text/plain",
            file_size=100,
            raw={},
        )
        msg = _msg("text", document=doc, media_group_id="mg1")
        c = _classify_message(msg, files_enabled=False)
        assert c.is_media_group_document is False


# ===========================================================================
# update_routing: TelegramUpdateRouter
# ===========================================================================


class TestTelegramUpdateRouter:
    @pytest.mark.anyio
    async def test_route_message_called(self) -> None:
        cfg = make_cfg(FakeTransport())
        state = _make_loop_state()
        route_called: list[Any] = []

        async def _route(update: Any) -> None:
            route_called.append(update)

        async with anyio.create_task_group() as tg:
            router = TelegramUpdateRouter(
                cfg=cfg,
                state=state,
                tg=tg,
                scheduler=AsyncMock(),
                route_message=_route,
            )
            msg = _msg("hello", update_id=1)
            await router.route_update(msg)

        assert len(route_called) == 1

    @pytest.mark.anyio
    async def test_duplicate_update_ignored(self) -> None:
        cfg = make_cfg(FakeTransport())
        state = _make_loop_state()
        route_called: list[Any] = []

        async def _route(update: Any) -> None:
            route_called.append(update)

        async with anyio.create_task_group() as tg:
            router = TelegramUpdateRouter(
                cfg=cfg,
                state=state,
                tg=tg,
                scheduler=AsyncMock(),
                route_message=_route,
            )
            msg = _msg("hello", update_id=42)
            await router.route_update(msg)
            await router.route_update(msg)

        assert len(route_called) == 1

    @pytest.mark.anyio
    async def test_duplicate_message_ignored_no_update_id(self) -> None:
        cfg = make_cfg(FakeTransport())
        state = _make_loop_state()
        route_called: list[Any] = []

        async def _route(update: Any) -> None:
            route_called.append(update)

        async with anyio.create_task_group() as tg:
            router = TelegramUpdateRouter(
                cfg=cfg,
                state=state,
                tg=tg,
                scheduler=AsyncMock(),
                route_message=_route,
            )
            msg = _msg("hello", update_id=None, message_id=10)
            await router.route_update(msg)
            await router.route_update(msg)

        assert len(route_called) == 1

    @pytest.mark.anyio
    async def test_allowed_user_ids_filter(self) -> None:
        cfg = replace(make_cfg(FakeTransport()), allowed_user_ids=(999,))
        state = _make_loop_state()
        route_called: list[Any] = []

        async def _route(update: Any) -> None:
            route_called.append(update)

        async with anyio.create_task_group() as tg:
            router = TelegramUpdateRouter(
                cfg=cfg,
                state=state,
                tg=tg,
                scheduler=AsyncMock(),
                route_message=_route,
            )
            msg = _msg("hello", sender_id=1, update_id=1)
            await router.route_update(msg)

        assert len(route_called) == 0

    @pytest.mark.anyio
    async def test_allowed_user_ids_allows(self) -> None:
        cfg = replace(make_cfg(FakeTransport()), allowed_user_ids=(1,))
        state = _make_loop_state()
        route_called: list[Any] = []

        async def _route(update: Any) -> None:
            route_called.append(update)

        async with anyio.create_task_group() as tg:
            router = TelegramUpdateRouter(
                cfg=cfg,
                state=state,
                tg=tg,
                scheduler=AsyncMock(),
                route_message=_route,
            )
            msg = _msg("hello", sender_id=1, update_id=1)
            await router.route_update(msg)

        assert len(route_called) == 1

    @pytest.mark.anyio
    async def test_allowed_user_none_sender_blocked(self) -> None:
        cfg = replace(make_cfg(FakeTransport()), allowed_user_ids=(999,))
        state = _make_loop_state()
        route_called: list[Any] = []

        async def _route(update: Any) -> None:
            route_called.append(update)

        async with anyio.create_task_group() as tg:
            router = TelegramUpdateRouter(
                cfg=cfg,
                state=state,
                tg=tg,
                scheduler=AsyncMock(),
                route_message=_route,
            )
            msg = _msg("hello", sender_id=None, update_id=1)
            await router.route_update(msg)

        assert len(route_called) == 0

    @pytest.mark.anyio
    async def test_callback_cancel(self) -> None:
        """Cancel callbacks should not call route_message."""
        transport = FakeTransport()
        cfg = make_cfg(transport)
        state = _make_loop_state()
        route_called: list[Any] = []
        cancel_dispatched = False

        async def _route(update: Any) -> None:
            route_called.append(update)

        async def _fake_cancel(*args: Any, **kwargs: Any) -> None:
            nonlocal cancel_dispatched
            cancel_dispatched = True

        with patch(
            "tunapi.telegram.commands.cancel.handle_callback_cancel",
            _fake_cancel,
        ):
            async with anyio.create_task_group() as tg:
                router = TelegramUpdateRouter(
                    cfg=cfg,
                    state=state,
                    tg=tg,
                    scheduler=AsyncMock(),
                    route_message=_route,
                )
                cb = _callback(data=CANCEL_CALLBACK_DATA, update_id=1)
                await router.route_update(cb)
                await anyio.sleep(0.01)

        assert len(route_called) == 0
        assert cancel_dispatched is True

    @pytest.mark.anyio
    async def test_callback_non_cancel(self) -> None:
        transport = FakeTransport()
        cfg = make_cfg(transport)
        bot = cfg.bot
        assert isinstance(bot, FakeBot)
        state = _make_loop_state()
        route_called: list[Any] = []

        async def _route(update: Any) -> None:
            route_called.append(update)

        async with anyio.create_task_group() as tg:
            router = TelegramUpdateRouter(
                cfg=cfg,
                state=state,
                tg=tg,
                scheduler=AsyncMock(),
                route_message=_route,
            )
            cb = _callback(data="other_data", update_id=2)
            await router.route_update(cb)
            await anyio.sleep(0.05)

        assert len(route_called) == 0
        assert len(bot.callback_calls) == 1

    @pytest.mark.anyio
    async def test_seen_update_eviction(self) -> None:
        """Oldest seen update IDs are evicted when limit is exceeded."""
        cfg = make_cfg(FakeTransport())
        state = _make_loop_state()
        route_called: list[Any] = []

        async def _route(update: Any) -> None:
            route_called.append(update)

        from tunapi.telegram.update_routing import _SEEN_UPDATES_LIMIT

        async with anyio.create_task_group() as tg:
            router = TelegramUpdateRouter(
                cfg=cfg,
                state=state,
                tg=tg,
                scheduler=AsyncMock(),
                route_message=_route,
            )
            # Fill past the limit
            for i in range(_SEEN_UPDATES_LIMIT + 5):
                msg = _msg("hi", update_id=i, message_id=i)
                await router.route_update(msg)

        assert len(state.seen_update_ids) <= _SEEN_UPDATES_LIMIT

    @pytest.mark.anyio
    async def test_seen_message_eviction(self) -> None:
        """Oldest seen message keys are evicted when limit is exceeded."""
        cfg = make_cfg(FakeTransport())
        state = _make_loop_state()

        from tunapi.telegram.update_routing import _SEEN_MESSAGES_LIMIT

        async def _route(update: Any) -> None:
            pass

        async with anyio.create_task_group() as tg:
            router = TelegramUpdateRouter(
                cfg=cfg,
                state=state,
                tg=tg,
                scheduler=AsyncMock(),
                route_message=_route,
            )
            for i in range(_SEEN_MESSAGES_LIMIT + 5):
                msg = _msg("hi", update_id=None, message_id=i)
                await router.route_update(msg)

        assert len(state.seen_message_keys) <= _SEEN_MESSAGES_LIMIT


# ===========================================================================
# commands/topics: _parse_chat_ctx_args
# ===========================================================================


class TestParseChatCtxArgs:
    def _runtime(self, tmp_path: Path) -> TransportRuntime:
        rt, _ = _runtime(tmp_path)
        return rt

    def test_empty_args(self, tmp_path: Path) -> None:
        rt = self._runtime(tmp_path)
        ctx, err = _parse_chat_ctx_args("", runtime=rt, default_project=None)
        assert ctx is None
        assert err is not None  # usage hint

    def test_project_only(self, tmp_path: Path) -> None:
        rt = self._runtime(tmp_path)
        ctx, err = _parse_chat_ctx_args("alpha", runtime=rt, default_project=None)
        assert err is None
        assert ctx is not None
        assert ctx.project == "alpha"
        assert ctx.branch is None

    def test_project_and_branch(self, tmp_path: Path) -> None:
        rt = self._runtime(tmp_path)
        ctx, err = _parse_chat_ctx_args("alpha @main", runtime=rt, default_project=None)
        assert err is None
        assert ctx is not None
        assert ctx.project == "alpha"
        assert ctx.branch == "main"

    def test_branch_only_with_default(self, tmp_path: Path) -> None:
        rt = self._runtime(tmp_path)
        ctx, err = _parse_chat_ctx_args("@feature", runtime=rt, default_project="alpha")
        assert err is None
        assert ctx is not None
        assert ctx.project == "alpha"
        assert ctx.branch == "feature"

    def test_branch_only_no_default(self, tmp_path: Path) -> None:
        rt = self._runtime(tmp_path)
        ctx, err = _parse_chat_ctx_args("@feature", runtime=rt, default_project=None)
        assert ctx is None
        assert err is not None
        assert "project is required" in err

    def test_too_many_args(self, tmp_path: Path) -> None:
        rt = self._runtime(tmp_path)
        ctx, err = _parse_chat_ctx_args("a b c", runtime=rt, default_project=None)
        assert ctx is None
        assert "too many" in err

    def test_branch_without_prefix(self, tmp_path: Path) -> None:
        rt = self._runtime(tmp_path)
        ctx, err = _parse_chat_ctx_args("alpha main", runtime=rt, default_project=None)
        assert ctx is None
        assert "prefixed with @" in err

    def test_unknown_project(self, tmp_path: Path) -> None:
        rt = self._runtime(tmp_path)
        ctx, err = _parse_chat_ctx_args("nonexistent", runtime=rt, default_project=None)
        assert ctx is None
        assert "unknown project" in err


# ===========================================================================
# commands/topics: _handle_chat_ctx_command
# ===========================================================================


def _sent_texts(transport: FakeTransport) -> list[str]:
    """Extract text from FakeTransport send calls."""
    return [c["message"].text for c in transport.send_calls]


class TestHandleChatCtxCommand:
    @pytest.mark.anyio
    async def test_no_prefs_store(self) -> None:
        transport = FakeTransport()
        cfg = make_cfg(transport)
        msg = _msg("/ctx")
        await _handle_chat_ctx_command(cfg, msg, "", chat_prefs=None)
        assert any("unavailable" in t for t in _sent_texts(transport))

    @pytest.mark.anyio
    async def test_show_no_bound(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        rt, state_path = _runtime(tmp_path)
        cfg = replace(make_cfg(transport), runtime=rt)
        prefs = ChatPrefsStore(tmp_path / "prefs.json")
        msg = _msg("/ctx")
        await _handle_chat_ctx_command(cfg, msg, "", chat_prefs=prefs)
        texts = _sent_texts(transport)
        assert len(texts) == 1
        assert "bound ctx:" in texts[0]

    @pytest.mark.anyio
    async def test_set_and_show(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        rt, state_path = _runtime(tmp_path)
        cfg = replace(make_cfg(transport), runtime=rt)
        prefs = ChatPrefsStore(tmp_path / "prefs.json")
        msg = _msg("/ctx set alpha")
        await _handle_chat_ctx_command(cfg, msg, "set alpha", chat_prefs=prefs)
        assert any("bound" in t for t in _sent_texts(transport))

    @pytest.mark.anyio
    async def test_set_error(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        rt, _ = _runtime(tmp_path)
        cfg = replace(make_cfg(transport), runtime=rt)
        prefs = ChatPrefsStore(tmp_path / "prefs.json")
        msg = _msg("/ctx set")
        await _handle_chat_ctx_command(cfg, msg, "set", chat_prefs=prefs)
        assert any("error" in t for t in _sent_texts(transport))

    @pytest.mark.anyio
    async def test_clear(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        rt, _ = _runtime(tmp_path)
        cfg = replace(make_cfg(transport), runtime=rt)
        prefs = ChatPrefsStore(tmp_path / "prefs.json")
        msg = _msg("/ctx clear")
        await _handle_chat_ctx_command(cfg, msg, "clear", chat_prefs=prefs)
        assert any("cleared" in t for t in _sent_texts(transport))

    @pytest.mark.anyio
    async def test_unknown_action(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        rt, _ = _runtime(tmp_path)
        cfg = replace(make_cfg(transport), runtime=rt)
        prefs = ChatPrefsStore(tmp_path / "prefs.json")
        msg = _msg("/ctx bogus")
        await _handle_chat_ctx_command(cfg, msg, "bogus", chat_prefs=prefs)
        assert any("unknown" in t for t in _sent_texts(transport))


# ===========================================================================
# commands/topics: _handle_chat_new_command
# ===========================================================================


class TestHandleChatNewCommand:
    @pytest.mark.anyio
    async def test_no_session_key(self) -> None:
        transport = FakeTransport()
        cfg = make_cfg(transport)
        msg = _msg("/new")
        store = ChatSessionStore(Path("/tmp/fake_sessions.json"))
        await _handle_chat_new_command(cfg, msg, store, session_key=None)
        assert any("no stored sessions" in t for t in _sent_texts(transport))

    @pytest.mark.anyio
    async def test_clear_private(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        cfg = make_cfg(transport)
        msg = _msg("/new", chat_type="private")
        store = ChatSessionStore(tmp_path / "sessions.json")
        await _handle_chat_new_command(cfg, msg, store, session_key=(123, None))
        assert any(
            "cleared stored sessions for this chat" in t for t in _sent_texts(transport)
        )

    @pytest.mark.anyio
    async def test_clear_group(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        cfg = make_cfg(transport)
        msg = _msg("/new", chat_type="supergroup")
        store = ChatSessionStore(tmp_path / "sessions.json")
        await _handle_chat_new_command(cfg, msg, store, session_key=(123, 1))
        assert any("for you in this chat" in t for t in _sent_texts(transport))


# ===========================================================================
# commands/topics: _handle_new_command (topic new)
# ===========================================================================


class TestHandleNewCommand:
    @pytest.mark.anyio
    async def test_requires_topic(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        cfg = replace(
            make_cfg(transport),
            topics=TelegramTopicsSettings(enabled=True, scope="all"),
        )
        store = TopicStateStore(tmp_path / "topics.json")
        msg = _msg("/new")
        await _handle_new_command(
            cfg,
            msg,
            store,
            resolved_scope="all",
            scope_chat_ids=frozenset({123}),
        )
        assert any("only works inside a topic" in t for t in _sent_texts(transport))

    @pytest.mark.anyio
    async def test_clears_sessions(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        cfg = replace(
            make_cfg(transport),
            topics=TelegramTopicsSettings(enabled=True, scope="all"),
        )
        store = TopicStateStore(tmp_path / "topics.json")
        msg = _msg("/new", thread_id=42, chat_type="supergroup", chat_id=123)
        await _handle_new_command(
            cfg,
            msg,
            store,
            resolved_scope="all",
            scope_chat_ids=frozenset({123}),
        )
        assert any("cleared" in t for t in _sent_texts(transport))


# ===========================================================================
# commands/topics: _handle_ctx_command (topic ctx)
# ===========================================================================


class TestHandleCtxCommand:
    @pytest.mark.anyio
    async def test_requires_topic(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        cfg = replace(
            make_cfg(transport),
            topics=TelegramTopicsSettings(enabled=True, scope="all"),
        )
        store = TopicStateStore(tmp_path / "topics.json")
        msg = _msg("/ctx")
        await _handle_ctx_command(
            cfg,
            msg,
            "",
            store,
            resolved_scope="all",
            scope_chat_ids=frozenset({123}),
        )
        assert any("only works inside a topic" in t for t in _sent_texts(transport))

    @pytest.mark.anyio
    async def test_show_default(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        rt, _ = _runtime(tmp_path)
        cfg = replace(
            make_cfg(transport),
            runtime=rt,
            topics=TelegramTopicsSettings(enabled=True, scope="all"),
        )
        store = TopicStateStore(tmp_path / "topics.json")
        msg = _msg("/ctx", thread_id=42, chat_type="supergroup", chat_id=123)
        await _handle_ctx_command(
            cfg,
            msg,
            "",
            store,
            resolved_scope="all",
            scope_chat_ids=frozenset({123}),
        )
        assert len(_sent_texts(transport)) >= 1

    @pytest.mark.anyio
    async def test_clear(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        rt, _ = _runtime(tmp_path)
        cfg = replace(
            make_cfg(transport),
            runtime=rt,
            topics=TelegramTopicsSettings(enabled=True, scope="all"),
        )
        store = TopicStateStore(tmp_path / "topics.json")
        msg = _msg("/ctx clear", thread_id=42, chat_type="supergroup", chat_id=123)
        await _handle_ctx_command(
            cfg,
            msg,
            "clear",
            store,
            resolved_scope="all",
            scope_chat_ids=frozenset({123}),
        )
        assert any("cleared" in t for t in _sent_texts(transport))

    @pytest.mark.anyio
    async def test_unknown_action(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        rt, _ = _runtime(tmp_path)
        cfg = replace(
            make_cfg(transport),
            runtime=rt,
            topics=TelegramTopicsSettings(enabled=True, scope="all"),
        )
        store = TopicStateStore(tmp_path / "topics.json")
        msg = _msg("/ctx bad", thread_id=42, chat_type="supergroup", chat_id=123)
        await _handle_ctx_command(
            cfg,
            msg,
            "bad",
            store,
            resolved_scope="all",
            scope_chat_ids=frozenset({123}),
        )
        assert any("unknown" in t for t in _sent_texts(transport))


# ===========================================================================
# commands/topics: _handle_topic_command
# ===========================================================================


class TestHandleTopicCommand:
    @pytest.mark.anyio
    async def test_topic_command_error_no_scope(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        cfg = replace(
            make_cfg(transport),
            topics=TelegramTopicsSettings(enabled=False),
        )
        store = TopicStateStore(tmp_path / "topics.json")
        msg = _msg("/topic alpha @main")
        await _handle_topic_command(
            cfg,
            msg,
            "alpha @main",
            store,
            resolved_scope=None,
            scope_chat_ids=frozenset(),
        )
        assert len(_sent_texts(transport)) >= 1

    @pytest.mark.anyio
    async def test_topic_command_creates_topic(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        rt, _ = _runtime(tmp_path)
        cfg = replace(
            make_cfg(transport),
            runtime=rt,
            topics=TelegramTopicsSettings(enabled=True, scope="all"),
        )
        store = TopicStateStore(tmp_path / "topics.json")
        msg = _msg("/topic alpha @main", chat_id=123)
        await _handle_topic_command(
            cfg,
            msg,
            "alpha @main",
            store,
            resolved_scope="all",
            scope_chat_ids=frozenset({123}),
        )
        assert any("created topic" in t for t in _sent_texts(transport))

    @pytest.mark.anyio
    async def test_topic_command_empty_args(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        rt, _ = _runtime(tmp_path)
        cfg = replace(
            make_cfg(transport),
            runtime=rt,
            topics=TelegramTopicsSettings(enabled=True, scope="all"),
        )
        store = TopicStateStore(tmp_path / "topics.json")
        msg = _msg("/topic", chat_id=123)
        await _handle_topic_command(
            cfg,
            msg,
            "",
            store,
            resolved_scope="all",
            scope_chat_ids=frozenset({123}),
        )
        # Should show usage or error
        assert len(_sent_texts(transport)) >= 1


# ===========================================================================
# Onboarding / Setup Tests
# ===========================================================================


class TestTelegramCheckSetup:
    def _make_backend(self) -> Any:
        from tunapi.backends import EngineBackend

        return EngineBackend(
            id="claude",
            build_runner=MagicMock(),
            cli_cmd="claude",
            install_cmd="npm i claude",
        )

    def test_telegram_configured(self, monkeypatch):
        settings = MagicMock()
        settings.transport = "telegram"
        with (
            patch(
                "tunapi.telegram.onboarding.load_settings",
                return_value=(settings, Path("/c.toml")),
            ),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("tunapi.telegram.onboarding.require_telegram"),
        ):
            result = tg_check_setup(self._make_backend())
        assert result.ok

    def test_telegram_config_error(self, monkeypatch):
        from tunapi.config import ConfigError

        settings = MagicMock()
        settings.transport = "telegram"
        with (
            patch(
                "tunapi.telegram.onboarding.load_settings",
                return_value=(settings, Path("/c.toml")),
            ),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "tunapi.telegram.onboarding.require_telegram",
                side_effect=ConfigError("bad"),
            ),
        ):
            result = tg_check_setup(self._make_backend())
        assert not result.ok

    def test_load_settings_error_file_exists(self, tmp_path: Path):
        from tunapi.config import ConfigError

        cfg = tmp_path / "tunapi.toml"
        cfg.touch()
        with (
            patch(
                "tunapi.telegram.onboarding.load_settings",
                side_effect=ConfigError("bad"),
            ),
            patch("tunapi.telegram.onboarding.HOME_CONFIG_PATH", cfg),
            patch("shutil.which", return_value=None),
        ):
            result = tg_check_setup(self._make_backend())
        assert not result.ok
        titles = [i.title for i in result.issues]
        assert "configure telegram" in titles

    def test_load_settings_error_no_file(self, tmp_path: Path):
        from tunapi.config import ConfigError

        cfg = tmp_path / "nonexistent.toml"
        with (
            patch(
                "tunapi.telegram.onboarding.load_settings",
                side_effect=ConfigError("bad"),
            ),
            patch("tunapi.telegram.onboarding.HOME_CONFIG_PATH", cfg),
            patch("shutil.which", return_value="/usr/bin/claude"),
        ):
            result = tg_check_setup(self._make_backend())
        titles = [i.title for i in result.issues]
        assert "create a config" in titles

    def test_transport_override_non_telegram(self, tmp_path: Path):
        from tunapi.config import ConfigError

        cfg = tmp_path / "nonexistent.toml"
        with (
            patch(
                "tunapi.telegram.onboarding.load_settings",
                side_effect=ConfigError("bad"),
            ),
            patch("tunapi.telegram.onboarding.HOME_CONFIG_PATH", cfg),
            patch("shutil.which", return_value="/usr/bin/claude"),
        ):
            result = tg_check_setup(self._make_backend(), transport_override="slack")
        titles = [i.title for i in result.issues]
        assert "create a config" in titles

    def test_non_telegram_transport(self):
        settings = MagicMock()
        settings.transport = "slack"
        with (
            patch(
                "tunapi.telegram.onboarding.load_settings",
                return_value=(settings, Path("/c.toml")),
            ),
            patch("shutil.which", return_value="/usr/bin/claude"),
        ):
            result = tg_check_setup(self._make_backend())
        assert result.ok  # No telegram-specific issues


class TestGetBotInfo:
    @pytest.mark.anyio
    async def test_success(self):
        user = User(id=1, is_bot=True, first_name="Bot")
        with patch("tunapi.telegram.onboarding.TelegramClient") as mock_cls:
            mock_bot = AsyncMock()
            mock_bot.get_me = AsyncMock(return_value=user)
            mock_bot.close = AsyncMock()
            mock_cls.return_value = mock_bot
            result = await get_bot_info("tok123")
        assert result is not None
        assert result.first_name == "Bot"

    @pytest.mark.anyio
    async def test_retry_on_rate_limit(self):
        from tunapi.telegram.client import TelegramRetryAfter

        user = User(id=1, is_bot=True, first_name="Bot")
        with patch("tunapi.telegram.onboarding.TelegramClient") as mock_cls:
            mock_bot = AsyncMock()
            mock_bot.get_me = AsyncMock(side_effect=[TelegramRetryAfter(0.01), user])
            mock_bot.close = AsyncMock()
            mock_cls.return_value = mock_bot
            result = await get_bot_info("tok", sleep=AsyncMock())
        assert result is not None

    @pytest.mark.anyio
    async def test_all_retries_fail(self):
        from tunapi.telegram.client import TelegramRetryAfter

        with patch("tunapi.telegram.onboarding.TelegramClient") as mock_cls:
            mock_bot = AsyncMock()
            mock_bot.get_me = AsyncMock(side_effect=[TelegramRetryAfter(0.01)] * 3)
            mock_bot.close = AsyncMock()
            mock_cls.return_value = mock_bot
            result = await get_bot_info("tok", sleep=AsyncMock())
        assert result is None


class TestWaitForChat:
    @pytest.mark.anyio
    async def test_receives_message(self):
        chat_obj = MagicMock()
        chat_obj.id = 42
        chat_obj.username = "user"
        chat_obj.title = None
        chat_obj.first_name = "Alice"
        chat_obj.last_name = None
        chat_obj.type = "private"

        msg_obj = MagicMock()
        msg_obj.from_ = MagicMock()
        msg_obj.from_.is_bot = False
        msg_obj.chat = chat_obj

        update = MagicMock()
        update.update_id = 1
        update.message = msg_obj

        with patch("tunapi.telegram.onboarding.TelegramClient") as mock_cls:
            mock_bot = AsyncMock()
            mock_bot.get_updates = AsyncMock(side_effect=[[], [update]])
            mock_bot.close = AsyncMock()
            mock_cls.return_value = mock_bot
            result = await wait_for_chat("tok", sleep=AsyncMock())
        assert result.chat_id == 42
        assert result.username == "user"

    @pytest.mark.anyio
    async def test_skips_bot_messages(self):
        # First update has bot sender, second has human
        bot_msg = MagicMock()
        bot_msg.from_ = MagicMock()
        bot_msg.from_.is_bot = True

        chat_obj = MagicMock()
        chat_obj.id = 99
        chat_obj.username = None
        chat_obj.title = None
        chat_obj.first_name = "Bob"
        chat_obj.last_name = None
        chat_obj.type = "private"

        human_msg = MagicMock()
        human_msg.from_ = MagicMock()
        human_msg.from_.is_bot = False
        human_msg.chat = chat_obj

        upd1 = MagicMock()
        upd1.update_id = 1
        upd1.message = bot_msg

        upd2 = MagicMock()
        upd2.update_id = 2
        upd2.message = human_msg

        with patch("tunapi.telegram.onboarding.TelegramClient") as mock_cls:
            mock_bot = AsyncMock()
            mock_bot.get_updates = AsyncMock(side_effect=[[], [upd1], [upd2]])
            mock_bot.close = AsyncMock()
            mock_cls.return_value = mock_bot
            result = await wait_for_chat("tok", sleep=AsyncMock())
        assert result.chat_id == 99

    @pytest.mark.anyio
    async def test_skips_none_message(self):
        chat_obj = MagicMock()
        chat_obj.id = 77
        chat_obj.username = None
        chat_obj.title = None
        chat_obj.first_name = None
        chat_obj.last_name = None
        chat_obj.type = "private"

        none_upd = MagicMock()
        none_upd.update_id = 1
        none_upd.message = None

        real_msg = MagicMock()
        real_msg.from_ = None
        real_msg.chat = chat_obj

        real_upd = MagicMock()
        real_upd.update_id = 2
        real_upd.message = real_msg

        with patch("tunapi.telegram.onboarding.TelegramClient") as mock_cls:
            mock_bot = AsyncMock()
            mock_bot.get_updates = AsyncMock(side_effect=[[], [none_upd], [real_upd]])
            mock_bot.close = AsyncMock()
            mock_cls.return_value = mock_bot
            result = await wait_for_chat("tok", sleep=AsyncMock())
        assert result.chat_id == 77

    @pytest.mark.anyio
    async def test_skips_none_updates(self):
        chat_obj = MagicMock()
        chat_obj.id = 55
        chat_obj.username = None
        chat_obj.title = None
        chat_obj.first_name = None
        chat_obj.last_name = None
        chat_obj.type = "private"

        real_msg = MagicMock()
        real_msg.from_ = None
        real_msg.chat = chat_obj

        real_upd = MagicMock()
        real_upd.update_id = 1
        real_upd.message = real_msg

        with patch("tunapi.telegram.onboarding.TelegramClient") as mock_cls:
            mock_bot = AsyncMock()
            mock_bot.get_updates = AsyncMock(side_effect=[[], None, [real_upd]])
            mock_bot.close = AsyncMock()
            mock_cls.return_value = mock_bot
            result = await wait_for_chat("tok", sleep=AsyncMock())
        assert result.chat_id == 55

    @pytest.mark.anyio
    async def test_drains_backlog(self):
        drain_upd = MagicMock()
        drain_upd.update_id = 5

        chat_obj = MagicMock()
        chat_obj.id = 88
        chat_obj.username = None
        chat_obj.title = None
        chat_obj.first_name = None
        chat_obj.last_name = None
        chat_obj.type = "private"

        msg = MagicMock()
        msg.from_ = None
        msg.chat = chat_obj

        real_upd = MagicMock()
        real_upd.update_id = 6
        real_upd.message = msg

        with patch("tunapi.telegram.onboarding.TelegramClient") as mock_cls:
            mock_bot = AsyncMock()
            mock_bot.get_updates = AsyncMock(side_effect=[[drain_upd], [real_upd]])
            mock_bot.close = AsyncMock()
            mock_cls.return_value = mock_bot
            result = await wait_for_chat("tok", sleep=AsyncMock())
        assert result.chat_id == 88


class TestValidateTopicsOnboarding:
    @pytest.mark.anyio
    async def test_success(self):
        with patch("tunapi.telegram.onboarding.TelegramClient") as mock_cls:
            mock_bot = AsyncMock()
            mock_bot.close = AsyncMock()
            mock_cls.return_value = mock_bot
            with patch(
                "tunapi.telegram.onboarding._validate_topics_setup_for", AsyncMock()
            ):
                result = await validate_topics_onboarding("tok", 123, "auto", ())
        assert result is None

    @pytest.mark.anyio
    async def test_config_error(self):
        from tunapi.config import ConfigError

        with patch("tunapi.telegram.onboarding.TelegramClient") as mock_cls:
            mock_bot = AsyncMock()
            mock_bot.close = AsyncMock()
            mock_cls.return_value = mock_bot
            with patch(
                "tunapi.telegram.onboarding._validate_topics_setup_for",
                AsyncMock(side_effect=ConfigError("bad topics")),
            ):
                result = await validate_topics_onboarding("tok", 123, "auto", ())
        assert result is not None
        assert "bad topics" in str(result)

    @pytest.mark.anyio
    async def test_generic_error(self):
        with patch("tunapi.telegram.onboarding.TelegramClient") as mock_cls:
            mock_bot = AsyncMock()
            mock_bot.close = AsyncMock()
            mock_cls.return_value = mock_bot
            with patch(
                "tunapi.telegram.onboarding._validate_topics_setup_for",
                AsyncMock(side_effect=RuntimeError("oops")),
            ):
                result = await validate_topics_onboarding("tok", 123, "auto", ())
        assert result is not None
        assert "oops" in str(result)


# ===========================================================================
# Backlog and resume helpers
# ===========================================================================

from tunapi.telegram.loop import (
    _drain_backlog,
    _wait_for_resume,
    _send_startup,
    send_with_resume,
)
from tunapi.telegram.builtin_commands import dispatch_builtin_command
from tunapi.model import ResumeToken


class TestDrainBacklog:
    @pytest.mark.anyio
    async def test_no_updates(self):
        cfg = MagicMock()
        cfg.bot.get_updates = AsyncMock(return_value=[])
        result = await _drain_backlog(cfg, None)
        assert result is None

    @pytest.mark.anyio
    async def test_failed(self):
        cfg = MagicMock()
        cfg.bot.get_updates = AsyncMock(return_value=None)
        result = await _drain_backlog(cfg, None)
        assert result is None

    @pytest.mark.anyio
    async def test_drain_multiple(self):
        upd1 = MagicMock()
        upd1.update_id = 10
        upd2 = MagicMock()
        upd2.update_id = 11
        cfg = MagicMock()
        cfg.bot.get_updates = AsyncMock(side_effect=[[upd1, upd2], []])
        result = await _drain_backlog(cfg, None)
        assert result == 12  # last update_id + 1


class TestWaitForResume:
    @pytest.mark.anyio
    async def test_resume_already_available(self):
        task = MagicMock()
        task.resume = ResumeToken(engine="claude", value="tok")
        result = await _wait_for_resume(task)
        assert result is not None
        assert result.value == "tok"

    @pytest.mark.anyio
    async def test_resume_set_later(self):
        resume_ready = anyio.Event()
        done = anyio.Event()
        task = MagicMock()
        task.resume = None
        task.resume_ready = resume_ready
        task.done = done

        async def set_resume():
            task.resume = ResumeToken(engine="claude", value="later")
            resume_ready.set()

        async with anyio.create_task_group() as tg:

            async def run():
                nonlocal task
                result = await _wait_for_resume(task)
                assert result is not None
                assert result.value == "later"

            tg.start_soon(run)
            tg.start_soon(set_resume)

    @pytest.mark.anyio
    async def test_done_before_resume(self):
        resume_ready = anyio.Event()
        done = anyio.Event()
        task = MagicMock()
        task.resume = None
        task.resume_ready = resume_ready
        task.done = done

        async def set_done():
            done.set()

        async with anyio.create_task_group() as tg:

            async def run():
                result = await _wait_for_resume(task)
                assert result is None

            tg.start_soon(run)
            tg.start_soon(set_done)


class TestSendStartup:
    @pytest.mark.anyio
    async def test_sends_message(self):
        cfg = MagicMock()
        cfg.startup_msg = "bot started"
        cfg.chat_id = 123
        cfg.exec_cfg.transport.send = AsyncMock(return_value=MagicMock())
        await _send_startup(cfg)
        cfg.exec_cfg.transport.send.assert_called_once()
        call_kwargs = cfg.exec_cfg.transport.send.call_args.kwargs
        assert call_kwargs["channel_id"] == 123

    @pytest.mark.anyio
    async def test_send_returns_none(self):
        cfg = MagicMock()
        cfg.startup_msg = "hi"
        cfg.chat_id = 1
        cfg.exec_cfg.transport.send = AsyncMock(return_value=None)
        await _send_startup(cfg)  # should not raise


class TestSendWithResume:
    @pytest.mark.anyio
    async def test_no_resume(self):
        cfg = MagicMock()
        cfg.exec_cfg.transport = AsyncMock()

        done = anyio.Event()
        done.set()

        task = type(
            "FakeTask",
            (),
            {
                "resume": None,
                "resume_ready": anyio.Event(),
                "done": done,
                "context": None,
            },
        )()

        enqueue = AsyncMock()

        await send_with_resume(
            cfg,
            enqueue,
            task,
            chat_id=1,
            user_msg_id=2,
            thread_id=None,
            session_key=None,
            text="hello",
        )
        enqueue.assert_not_called()

    @pytest.mark.anyio
    async def test_with_resume(self):
        cfg = MagicMock()
        cfg.exec_cfg.transport = AsyncMock()
        cfg.exec_cfg.transport.send = AsyncMock(return_value=MagicMock())

        task = type(
            "FakeTask",
            (),
            {
                "resume": ResumeToken(engine="claude", value="tok"),
                "context": None,
            },
        )()

        enqueue = AsyncMock()

        with patch(
            "tunapi.telegram.loop._send_queued_progress", AsyncMock(return_value=None)
        ):
            await send_with_resume(
                cfg,
                enqueue,
                task,
                chat_id=1,
                user_msg_id=2,
                thread_id=None,
                session_key=None,
                text="hello",
            )
        enqueue.assert_called_once()


def _make_cmd_ctx(
    command_id: str = "file",
    *,
    topics_enabled: bool = False,
    files_enabled: bool = True,
    topic_store: Any = None,
    chat_prefs: Any = None,
) -> Any:
    cfg = MagicMock()
    cfg.topics.enabled = topics_enabled
    cfg.files.enabled = files_enabled
    msg = _msg()
    tg = MagicMock()
    tg.start_soon = MagicMock()
    ctx = MagicMock()
    ctx.cfg = cfg
    ctx.msg = msg
    ctx.args_text = ""
    ctx.ambient_context = None
    ctx.topic_store = topic_store
    ctx.chat_prefs = chat_prefs
    ctx.resolved_scope = None
    ctx.scope_chat_ids = frozenset()
    ctx.reply = AsyncMock()
    ctx.task_group = tg
    return ctx


class TestDispatchBuiltinCommand:
    def test_file_disabled(self):
        ctx = _make_cmd_ctx("file", files_enabled=False)
        assert dispatch_builtin_command(ctx=ctx, command_id="file") is True
        ctx.task_group.start_soon.assert_called_once()

    def test_file_enabled(self):
        ctx = _make_cmd_ctx("file", files_enabled=True)
        assert dispatch_builtin_command(ctx=ctx, command_id="file") is True

    def test_ctx_no_topics(self):
        ctx = _make_cmd_ctx("ctx")
        assert dispatch_builtin_command(ctx=ctx, command_id="ctx") is True

    def test_ctx_with_topics_no_thread(self):
        ctx = _make_cmd_ctx("ctx", topics_enabled=True, topic_store=MagicMock())
        assert dispatch_builtin_command(ctx=ctx, command_id="ctx") is True

    def test_model(self):
        ctx = _make_cmd_ctx("model")
        assert dispatch_builtin_command(ctx=ctx, command_id="model") is True

    def test_agent(self):
        ctx = _make_cmd_ctx("agent")
        assert dispatch_builtin_command(ctx=ctx, command_id="agent") is True

    def test_reasoning(self):
        ctx = _make_cmd_ctx("reasoning")
        assert dispatch_builtin_command(ctx=ctx, command_id="reasoning") is True

    def test_trigger(self):
        ctx = _make_cmd_ctx("trigger")
        assert dispatch_builtin_command(ctx=ctx, command_id="trigger") is True

    def test_unknown(self):
        ctx = _make_cmd_ctx("unknown")
        assert dispatch_builtin_command(ctx=ctx, command_id="unknown") is False

    def test_new_with_topics(self):
        ctx = _make_cmd_ctx("new", topics_enabled=True, topic_store=MagicMock())
        assert dispatch_builtin_command(ctx=ctx, command_id="new") is True

    def test_topic_with_topics(self):
        ctx = _make_cmd_ctx("topic", topics_enabled=True, topic_store=MagicMock())
        assert dispatch_builtin_command(ctx=ctx, command_id="topic") is True

    def test_new_without_topics(self):
        ctx = _make_cmd_ctx("new", topics_enabled=False)
        assert dispatch_builtin_command(ctx=ctx, command_id="new") is False

    def test_topic_without_topics_returns_false(self):
        ctx = _make_cmd_ctx("other", topics_enabled=True, topic_store=MagicMock())
        assert dispatch_builtin_command(ctx=ctx, command_id="other") is False
