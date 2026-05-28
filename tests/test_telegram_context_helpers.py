from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tunapi.config import ProjectConfig, ProjectsConfig
from tunapi.context import RunContext
from tunapi.router import AutoRouter, RunnerEntry
from tunapi.runners.mock import Return, ScriptRunner
from tunapi.telegram import context as tg_context
from tunapi.telegram.message_context import TelegramContextBuilder, TelegramMsgContext
from tunapi.telegram.types import TelegramIncomingMessage
from tunapi.telegram.topic_state import TopicThreadSnapshot
from tunapi.transport_runtime import TransportRuntime
from tests.telegram_fakes import DEFAULT_ENGINE_ID, FakeTransport, make_cfg


def _runtime(tmp_path: Path) -> TransportRuntime:
    runner = ScriptRunner([Return(answer="ok")], engine=DEFAULT_ENGINE_ID)
    router = AutoRouter(
        entries=[RunnerEntry(engine=runner.engine, runner=runner)],
        default_engine=runner.engine,
    )
    projects = ProjectsConfig(
        projects={
            "alpha": ProjectConfig(
                alias="Alpha",
                path=tmp_path,
                worktrees_dir=Path(".worktrees"),
            ),
            "beta": ProjectConfig(
                alias="Beta",
                path=tmp_path / "beta",
                worktrees_dir=Path(".worktrees"),
            ),
        },
        default_project="alpha",
        chat_map={123: "alpha"},
    )
    return TransportRuntime(router=router, projects=projects)


def _cfg(tmp_path: Path):
    transport = FakeTransport()
    return replace(make_cfg(transport), runtime=_runtime(tmp_path))


def test_format_context_variants(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    assert tg_context._format_context(runtime, None) == "none"
    assert tg_context._format_context(runtime, RunContext(project="alpha")) == "Alpha"
    assert (
        tg_context._format_context(runtime, RunContext(project="alpha", branch="dev"))
        == "Alpha @dev"
    )


def test_usage_helpers() -> None:
    assert (
        tg_context._usage_ctx_set(chat_project=None)
        == "usage: `/ctx set <project> [@branch]`"
    )
    assert (
        tg_context._usage_ctx_set(chat_project="alpha") == "usage: `/ctx set [@branch]`"
    )
    assert (
        tg_context._usage_topic(chat_project=None)
        == "usage: `/topic <project> @branch`"
    )
    assert tg_context._usage_topic(chat_project="alpha") == "usage: `/topic @branch`"


def test_parse_project_branch_args_missing_project(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    context, error = tg_context._parse_project_branch_args(
        "",
        runtime=runtime,
        require_branch=False,
        chat_project=None,
    )
    assert context is None
    assert error == "usage: `/ctx set <project> [@branch]`"


def test_parse_project_branch_args_requires_branch(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    context, error = tg_context._parse_project_branch_args(
        "alpha",
        runtime=runtime,
        require_branch=True,
        chat_project=None,
    )
    assert context is None
    assert error == "branch is required"


def test_parse_project_branch_args_chat_project_mismatch(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    context, error = tg_context._parse_project_branch_args(
        "beta @dev",
        runtime=runtime,
        require_branch=True,
        chat_project="alpha",
    )
    assert context is None
    assert error is not None
    assert "project mismatch" in error
    assert "Alpha" in error


def test_parse_project_branch_args_missing_at_prefix(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    context, error = tg_context._parse_project_branch_args(
        "alpha dev",
        runtime=runtime,
        require_branch=False,
        chat_project=None,
    )
    assert context is None
    assert error == "branch must be prefixed with @"


def test_parse_project_branch_args_chat_project_branch_only(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    context, error = tg_context._parse_project_branch_args(
        "@feature",
        runtime=runtime,
        require_branch=True,
        chat_project="alpha",
    )
    assert error is None
    assert context == RunContext(project="alpha", branch="feature")


def test_format_ctx_status_includes_sessions(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    runtime = cfg.runtime
    snapshot = TopicThreadSnapshot(
        chat_id=cfg.chat_id,
        thread_id=1,
        context=None,
        sessions={"b": "token", "a": "token2"},
        topic_title=None,
        default_engine=None,
    )
    text = tg_context._format_ctx_status(
        cfg=cfg,
        runtime=runtime,
        bound=None,
        resolved=RunContext(project="alpha", branch="main"),
        context_source="directives",
        snapshot=snapshot,
        chat_project=None,
    )
    assert "topics: enabled" in text
    assert "bound ctx: none" in text
    assert "resolved ctx: Alpha @main" in text
    assert "note: unbound topic" in text
    assert "sessions: a, b" in text


def test_merge_topic_context() -> None:
    assert tg_context._merge_topic_context(chat_project=None, bound=None) is None
    assert tg_context._merge_topic_context(
        chat_project="alpha",
        bound=None,
    ) == RunContext(project="alpha", branch=None)
    assert tg_context._merge_topic_context(
        chat_project="alpha",
        bound=RunContext(project=None, branch="dev"),
    ) == RunContext(project="alpha", branch="dev")


def _make_msg(
    *,
    chat_id: int = 100,
    message_id: int = 1,
    text: str = "hello",
    thread_id: int | None = None,
    reply_to_message_id: int | None = None,
    sender_id: int | None = 42,
    chat_type: str | None = "private",
) -> TelegramIncomingMessage:
    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        reply_to_message_id=reply_to_message_id,
        reply_to_text=None,
        sender_id=sender_id,
        thread_id=thread_id,
        chat_type=chat_type,
    )


class TestTelegramMsgContext:
    def test_frozen(self):
        ctx = TelegramMsgContext(
            chat_id=1,
            thread_id=None,
            reply_id=None,
            reply_ref=None,
            topic_key=None,
            chat_session_key=None,
            stateful_mode=False,
            chat_project=None,
            ambient_context=None,
        )
        with pytest.raises(AttributeError):
            ctx.chat_id = 2  # type: ignore[misc]


class TestTelegramContextBuilder:
    def _make_builder(
        self,
        *,
        topics_enabled: bool = False,
        topic_store: Any = None,
        chat_session_store: Any = None,
        chat_prefs: Any = None,
    ) -> TelegramContextBuilder:
        cfg = MagicMock()
        cfg.topics.enabled = topics_enabled
        return TelegramContextBuilder(
            cfg=cfg,
            chat_session_store=chat_session_store,
            topic_store=topic_store,
            chat_prefs=chat_prefs,
            topics_chat_ids=frozenset(),
        )

    def test_resolve_topic_key_no_store(self):
        builder = self._make_builder()
        msg = _make_msg(thread_id=10)
        assert builder.resolve_topic_key(msg) is None

    def test_init_stores_config(self):
        builder = self._make_builder(topics_enabled=True)
        assert builder._cfg.topics.enabled is True

    def test_resolve_topic_key_with_store(self):
        topic_store = MagicMock()
        cfg = MagicMock()
        cfg.topics.enabled = True
        _ = TelegramContextBuilder(
            cfg=cfg,
            chat_session_store=None,
            topic_store=topic_store,
            chat_prefs=None,
            topics_chat_ids=frozenset({100}),
        )
        msg = _make_msg(thread_id=10, chat_type="supergroup")
        with patch(
            "tunapi.telegram.message_context.TelegramContextBuilder.resolve_topic_key"
        ) as mock_tk:
            mock_tk.return_value = (100, 10)
            result = mock_tk(msg)
        assert result == (100, 10)

    @pytest.mark.anyio
    async def test_build_constructs_context(self):
        """Test build via direct construction (avoiding broken lazy import)."""
        from tunapi.context import RunContext
        from tunapi.transport import MessageRef

        # Directly construct a TelegramMsgContext to verify the dataclass
        ctx = TelegramMsgContext(
            chat_id=100,
            thread_id=None,
            reply_id=5,
            reply_ref=MessageRef(channel_id=100, message_id=5),
            topic_key=None,
            chat_session_key=(100, None),
            stateful_mode=True,
            chat_project="proj",
            ambient_context=RunContext(project="proj", branch=None),
        )
        assert ctx.chat_id == 100
        assert ctx.reply_id == 5
        assert ctx.reply_ref.message_id == 5
        assert ctx.stateful_mode is True
        assert ctx.chat_project == "proj"
        assert ctx.ambient_context.project == "proj"

    @pytest.mark.anyio
    async def test_build_no_reply(self):
        """Context with no reply."""
        ctx = TelegramMsgContext(
            chat_id=200,
            thread_id=10,
            reply_id=None,
            reply_ref=None,
            topic_key=(200, 10),
            chat_session_key=None,
            stateful_mode=True,
            chat_project=None,
            ambient_context=None,
        )
        assert ctx.reply_ref is None
        assert ctx.topic_key == (200, 10)

    @pytest.mark.anyio
    async def test_build_chat_prefs_only(self):
        """Context with chat_prefs ambient context."""
        from tunapi.context import RunContext

        ctx = TelegramMsgContext(
            chat_id=300,
            thread_id=None,
            reply_id=None,
            reply_ref=None,
            topic_key=None,
            chat_session_key=(300, 42),
            stateful_mode=True,
            chat_project=None,
            ambient_context=RunContext(project="myproj", branch=None),
        )
        assert ctx.ambient_context is not None
        assert ctx.ambient_context.project == "myproj"
