"""Tests for Telegram update_routing and commands/topics."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import anyio
import pytest

from tunapi.config import ProjectConfig, ProjectsConfig
from tunapi.context import RunContext
from tunapi.runners.mock import Return, ScriptRunner
from tunapi.settings import TelegramTopicsSettings
from tunapi.telegram.bridge import CANCEL_CALLBACK_DATA
from tunapi.telegram.chat_prefs import ChatPrefsStore
from tunapi.telegram.chat_sessions import ChatSessionStore
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
    MessageClassification,
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
            file_id="f1", file_name="test.txt", mime_type="text/plain",
            file_size=100, raw={},
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
            file_id="f1", file_name="test.txt", mime_type="text/plain",
            file_size=100, raw={},
        )
        msg = _msg("text", document=doc, media_group_id="mg1")
        c = _classify_message(msg, files_enabled=True)
        assert c.is_media_group_document is True

    def test_media_group_document_files_disabled(self) -> None:
        from tunapi.telegram.types import TelegramDocument

        doc = TelegramDocument(
            file_id="f1", file_name="test.txt", mime_type="text/plain",
            file_size=100, raw={},
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
                cfg=cfg, state=state, tg=tg,
                scheduler=AsyncMock(), route_message=_route,
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
                cfg=cfg, state=state, tg=tg,
                scheduler=AsyncMock(), route_message=_route,
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
                cfg=cfg, state=state, tg=tg,
                scheduler=AsyncMock(), route_message=_route,
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
                cfg=cfg, state=state, tg=tg,
                scheduler=AsyncMock(), route_message=_route,
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
                cfg=cfg, state=state, tg=tg,
                scheduler=AsyncMock(), route_message=_route,
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
                cfg=cfg, state=state, tg=tg,
                scheduler=AsyncMock(), route_message=_route,
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
                    cfg=cfg, state=state, tg=tg,
                    scheduler=AsyncMock(), route_message=_route,
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
                cfg=cfg, state=state, tg=tg,
                scheduler=AsyncMock(), route_message=_route,
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
                cfg=cfg, state=state, tg=tg,
                scheduler=AsyncMock(), route_message=_route,
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
                cfg=cfg, state=state, tg=tg,
                scheduler=AsyncMock(), route_message=_route,
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
        ctx, err = _parse_chat_ctx_args(
            "alpha @main", runtime=rt, default_project=None
        )
        assert err is None
        assert ctx is not None
        assert ctx.project == "alpha"
        assert ctx.branch == "main"

    def test_branch_only_with_default(self, tmp_path: Path) -> None:
        rt = self._runtime(tmp_path)
        ctx, err = _parse_chat_ctx_args(
            "@feature", runtime=rt, default_project="alpha"
        )
        assert err is None
        assert ctx is not None
        assert ctx.project == "alpha"
        assert ctx.branch == "feature"

    def test_branch_only_no_default(self, tmp_path: Path) -> None:
        rt = self._runtime(tmp_path)
        ctx, err = _parse_chat_ctx_args(
            "@feature", runtime=rt, default_project=None
        )
        assert ctx is None
        assert err is not None
        assert "project is required" in err

    def test_too_many_args(self, tmp_path: Path) -> None:
        rt = self._runtime(tmp_path)
        ctx, err = _parse_chat_ctx_args(
            "a b c", runtime=rt, default_project=None
        )
        assert ctx is None
        assert "too many" in err

    def test_branch_without_prefix(self, tmp_path: Path) -> None:
        rt = self._runtime(tmp_path)
        ctx, err = _parse_chat_ctx_args(
            "alpha main", runtime=rt, default_project=None
        )
        assert ctx is None
        assert "prefixed with @" in err

    def test_unknown_project(self, tmp_path: Path) -> None:
        rt = self._runtime(tmp_path)
        ctx, err = _parse_chat_ctx_args(
            "nonexistent", runtime=rt, default_project=None
        )
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
        assert any("cleared stored sessions for this chat" in t for t in _sent_texts(transport))

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
            cfg, msg, store,
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
            cfg, msg, store,
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
            cfg, msg, "", store,
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
            cfg, msg, "", store,
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
            cfg, msg, "clear", store,
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
            cfg, msg, "bad", store,
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
            cfg, msg, "alpha @main", store,
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
            cfg, msg, "alpha @main", store,
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
            cfg, msg, "", store,
            resolved_scope="all",
            scope_chat_ids=frozenset({123}),
        )
        # Should show usage or error
        assert len(_sent_texts(transport)) >= 1
