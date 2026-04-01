"""Tests for tunapi.tunadish.commands — command handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tunapi.context import RunContext
from tunapi.transport import RenderedMessage
from tunapi.tunadish.commands import (
    _MIN_PREFIX_LEN,
    _resolve_id,
    dispatch_command,
    handle_branch,
    handle_context,
    handle_help,
    handle_memory,
    handle_model,
    handle_models,
    handle_persona,
    handle_project,
    handle_review,
    handle_rt,
    handle_status,
    handle_trigger,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _make_send() -> AsyncMock:
    return AsyncMock()


def _sent_text(send: AsyncMock) -> str:
    """Return the text from the first send() call."""
    assert send.await_count >= 1
    msg: RenderedMessage = send.call_args_list[0].args[0]
    return msg.text


def _all_sent_texts(send: AsyncMock) -> list[str]:
    return [call.args[0].text for call in send.call_args_list]


def _make_runtime(
    engines: list[str] | None = None,
    projects: list[str] | None = None,
    default_engine: str = "claude",
) -> MagicMock:
    rt = MagicMock()
    rt.available_engine_ids.return_value = engines or ["claude", "codex"]
    rt.project_aliases.return_value = projects or []
    rt.default_engine = default_engine
    rt.normalize_project_key.return_value = None  # default: unknown project
    rt.roundtable = MagicMock()
    rt.roundtable.engines = []
    return rt


def _make_chat_prefs(**overrides: Any) -> AsyncMock:
    prefs = AsyncMock()
    prefs.get_default_engine = AsyncMock(return_value=overrides.get("engine"))
    prefs.get_trigger_mode = AsyncMock(return_value=overrides.get("trigger"))
    prefs.get_engine_model = AsyncMock(return_value=overrides.get("model"))
    prefs.get_all_engine_models = AsyncMock(return_value=overrides.get("all_models", {}))
    prefs.get_context = AsyncMock(return_value=overrides.get("context"))
    return prefs


@dataclass
class FakeEntry:
    id: str
    type: str
    title: str
    content: str = ""
    tags: list[str] | None = None
    timestamp: float = 1700000000.0
    source: str = "user"


@dataclass
class FakeBranch:
    branch_id: str
    label: str
    status: str = "active"
    git_branch: str | None = None


@dataclass
class FakeReview:
    review_id: str
    artifact_id: str
    artifact_version: int = 1
    status: str = "pending"


@dataclass
class FakePersona:
    name: str
    prompt: str


# ---------------------------------------------------------------------------
# !help
# ---------------------------------------------------------------------------


class TestHandleHelp:
    async def test_help_basic(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude", "codex"], projects=["myproj"])
        await handle_help(runtime=runtime, send=send)
        text = _sent_text(send)
        assert "tunapi commands" in text
        assert "`claude`" in text
        assert "`codex`" in text
        assert "`myproj`" in text

    async def test_help_no_engines_no_projects(self):
        send = _make_send()
        runtime = _make_runtime(engines=[], projects=[])
        await handle_help(runtime=runtime, send=send)
        text = _sent_text(send)
        assert "none" in text


# ---------------------------------------------------------------------------
# !model
# ---------------------------------------------------------------------------


class TestHandleModel:
    async def test_no_args_shows_current(self):
        send = _make_send()
        runtime = _make_runtime()
        prefs = _make_chat_prefs(engine="claude")
        await handle_model("", channel_id="ch1", runtime=runtime, chat_prefs=prefs, send=send)
        text = _sent_text(send)
        assert "Current engine" in text
        assert "`claude`" in text

    async def test_no_args_with_model_override(self):
        send = _make_send()
        runtime = _make_runtime()
        prefs = _make_chat_prefs(engine="claude", model="claude-opus-4-20250514")
        await handle_model("", channel_id="ch1", runtime=runtime, chat_prefs=prefs, send=send)
        text = _sent_text(send)
        assert "Model:" in text

    async def test_no_args_no_prefs(self):
        send = _make_send()
        runtime = _make_runtime(default_engine="codex")
        await handle_model("", channel_id="ch1", runtime=runtime, chat_prefs=None, send=send)
        text = _sent_text(send)
        assert "`codex`" in text

    async def test_unknown_engine(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude"])
        await handle_model("nope", channel_id="ch1", runtime=runtime, chat_prefs=None, send=send)
        text = _sent_text(send)
        assert "Unknown engine" in text
        assert "`nope`" in text

    async def test_set_engine_only(self):
        send = _make_send()
        runtime = _make_runtime()
        prefs = _make_chat_prefs()
        await handle_model("claude", channel_id="ch1", runtime=runtime, chat_prefs=prefs, send=send)
        prefs.set_default_engine.assert_awaited_once_with("ch1", "claude")
        text = _sent_text(send)
        assert "Default engine set to `claude`" in text

    async def test_set_engine_case_insensitive(self):
        send = _make_send()
        runtime = _make_runtime(engines=["Claude"])
        prefs = _make_chat_prefs()
        await handle_model("claude", channel_id="ch1", runtime=runtime, chat_prefs=prefs, send=send)
        prefs.set_default_engine.assert_awaited_once_with("ch1", "Claude")

    async def test_set_model(self):
        send = _make_send()
        runtime = _make_runtime()
        prefs = _make_chat_prefs()
        await handle_model(
            "claude claude-opus-4-20250514", channel_id="ch1",
            runtime=runtime, chat_prefs=prefs, send=send,
        )
        prefs.set_engine_model.assert_awaited_once_with("ch1", "claude", "claude-opus-4-20250514")
        text = _sent_text(send)
        assert "claude-opus-4-20250514" in text

    async def test_clear_model(self):
        send = _make_send()
        runtime = _make_runtime()
        prefs = _make_chat_prefs()
        await handle_model("claude clear", channel_id="ch1", runtime=runtime, chat_prefs=prefs, send=send)
        prefs.clear_engine_model.assert_awaited_once_with("ch1", "claude")
        text = _sent_text(send)
        assert "cleared" in text


# ---------------------------------------------------------------------------
# !models
# ---------------------------------------------------------------------------


class TestHandleModels:
    async def test_lists_all_engines(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude", "codex"])
        await handle_models("", channel_id="ch1", runtime=runtime, chat_prefs=None, send=send)
        text = _sent_text(send)
        assert "Available Models" in text

    async def test_unknown_engine(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude"])
        await handle_models("nope", channel_id="ch1", runtime=runtime, chat_prefs=None, send=send)
        text = _sent_text(send)
        assert "Unknown engine" in text

    async def test_specific_engine(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude", "codex"])
        await handle_models("claude", channel_id="ch1", runtime=runtime, chat_prefs=None, send=send)
        text = _sent_text(send)
        assert "claude" in text


# ---------------------------------------------------------------------------
# !trigger
# ---------------------------------------------------------------------------


class TestHandleTrigger:
    async def test_set_all(self):
        send = _make_send()
        prefs = _make_chat_prefs()
        await handle_trigger("all", channel_id="ch1", chat_prefs=prefs, send=send)
        prefs.set_trigger_mode.assert_awaited_once_with("ch1", "all")
        assert "respond to all messages" in _sent_text(send)

    async def test_set_mentions(self):
        send = _make_send()
        prefs = _make_chat_prefs()
        await handle_trigger("mentions", channel_id="ch1", chat_prefs=prefs, send=send)
        prefs.set_trigger_mode.assert_awaited_once_with("ch1", "mentions")
        assert "respond only when @mentioned" in _sent_text(send)

    async def test_invalid_shows_current(self):
        send = _make_send()
        prefs = _make_chat_prefs(trigger="all")
        await handle_trigger("invalid", channel_id="ch1", chat_prefs=prefs, send=send)
        text = _sent_text(send)
        assert "Current trigger mode" in text
        assert "`all`" in text

    async def test_no_prefs(self):
        send = _make_send()
        await handle_trigger("all", channel_id="ch1", chat_prefs=None, send=send)
        # should still send confirmation even without prefs
        assert "respond to all messages" in _sent_text(send)

    async def test_empty_args_shows_usage(self):
        send = _make_send()
        prefs = _make_chat_prefs()
        await handle_trigger("", channel_id="ch1", chat_prefs=prefs, send=send)
        assert "Usage" in _sent_text(send)


# ---------------------------------------------------------------------------
# !status
# ---------------------------------------------------------------------------


class TestHandleStatus:
    async def test_basic_status(self):
        send = _make_send()
        runtime = _make_runtime(default_engine="claude")
        prefs = _make_chat_prefs(engine="codex", trigger="all")
        await handle_status(channel_id="ch1", runtime=runtime, chat_prefs=prefs, send=send)
        text = _sent_text(send)
        assert "Session status" in text
        assert "`codex`" in text
        assert "`all`" in text
        assert "`ch1`" in text

    async def test_status_no_prefs(self):
        send = _make_send()
        runtime = _make_runtime(default_engine="claude")
        await handle_status(channel_id="ch1", runtime=runtime, chat_prefs=None, send=send)
        text = _sent_text(send)
        assert "`claude`" in text

    async def test_status_with_project(self):
        send = _make_send()
        runtime = _make_runtime()
        ctx = RunContext(project="myproj", branch="feat-x")
        prefs = _make_chat_prefs(context=ctx)
        await handle_status(channel_id="ch1", runtime=runtime, chat_prefs=prefs, send=send)
        text = _sent_text(send)
        assert "`myproj`" in text
        assert "feat-x" in text


# ---------------------------------------------------------------------------
# !project
# ---------------------------------------------------------------------------


class TestHandleProject:
    async def _call(self, args: str, **kwargs: Any) -> str:
        send = _make_send()
        defaults = dict(
            channel_id="ch1",
            runtime=_make_runtime(),
            chat_prefs=None,
            context_store=None,
            projects_root=None,
            config_path=None,
            send=send,
        )
        defaults.update(kwargs)
        await handle_project(args, **defaults)
        return _sent_text(send)

    async def test_unknown_subcmd(self):
        text = await self._call("foo")
        assert "Usage" in text

    async def test_list_no_projects(self):
        text = await self._call("list")
        assert "No projects found" in text

    async def test_list_with_configured(self):
        runtime = _make_runtime(projects=["alpha", "beta"])
        text = await self._call("list", runtime=runtime)
        assert "Configured" in text
        assert "`alpha`" in text

    async def test_set_missing_name(self):
        text = await self._call("set")
        assert "Usage" in text

    async def test_set_unknown_project(self):
        text = await self._call("set myproj")
        assert "Unknown project" in text

    async def test_set_known_project(self):
        runtime = _make_runtime()
        runtime.normalize_project_key.return_value = "myproj"
        ctx_store = AsyncMock()
        text = await self._call(
            "set myproj", runtime=runtime, context_store=ctx_store,
        )
        assert "Project set to `myproj`" in text
        ctx_store.set_context.assert_awaited_once()

    async def test_set_rpc_channel_skips_context_store(self):
        runtime = _make_runtime()
        runtime.normalize_project_key.return_value = "myproj"
        ctx_store = AsyncMock()
        text = await self._call(
            "set myproj", runtime=runtime, context_store=ctx_store,
            channel_id="__rpc__",
        )
        assert "Project set to `myproj`" in text
        ctx_store.set_context.assert_not_awaited()

    async def test_info_no_context(self):
        text = await self._call("info")
        assert "No project bound" in text

    async def test_info_with_context(self):
        ctx_store = AsyncMock()
        ctx_store.get_context = AsyncMock(
            return_value=RunContext(project="myproj", branch="dev"),
        )
        text = await self._call("info", context_store=ctx_store)
        assert "`myproj`" in text
        assert "`dev`" in text


# ---------------------------------------------------------------------------
# !persona
# ---------------------------------------------------------------------------


class TestHandlePersona:
    async def _call(self, args: str, prefs: Any = None) -> str:
        send = _make_send()
        await handle_persona(args, chat_prefs=prefs, send=send)
        return _sent_text(send)

    async def test_no_prefs(self):
        text = await self._call("")
        assert "unavailable" in text

    async def test_unknown_subcmd(self):
        prefs = AsyncMock()
        text = await self._call("xyz", prefs=prefs)
        assert "Usage" in text

    async def test_add_missing_args(self):
        prefs = AsyncMock()
        text = await self._call("add", prefs=prefs)
        assert "Usage" in text

    async def test_add_missing_prompt(self):
        prefs = AsyncMock()
        text = await self._call("add myname", prefs=prefs)
        assert "Usage" in text

    async def test_add_success(self):
        prefs = AsyncMock()
        text = await self._call('add reviewer "Be critical"', prefs=prefs)
        prefs.add_persona.assert_awaited_once_with("reviewer", "Be critical")
        assert "Persona `reviewer` added" in text

    async def test_list_empty(self):
        prefs = AsyncMock()
        prefs.list_personas = AsyncMock(return_value={})
        text = await self._call("list", prefs=prefs)
        assert "No personas defined" in text

    async def test_list_with_personas(self):
        prefs = AsyncMock()
        prefs.list_personas = AsyncMock(return_value={
            "critic": FakePersona("critic", "Be very critical"),
            "helper": FakePersona("helper", "Be helpful and kind"),
        })
        text = await self._call("list", prefs=prefs)
        assert "**critic**" in text
        assert "**helper**" in text

    async def test_list_truncates_long_prompt(self):
        prefs = AsyncMock()
        long_prompt = "x" * 100
        prefs.list_personas = AsyncMock(return_value={
            "verbose": FakePersona("verbose", long_prompt),
        })
        text = await self._call("list", prefs=prefs)
        assert "..." in text

    async def test_remove_missing_name(self):
        prefs = AsyncMock()
        text = await self._call("remove", prefs=prefs)
        assert "Usage" in text

    async def test_remove_success(self):
        prefs = AsyncMock()
        prefs.remove_persona = AsyncMock(return_value=True)
        text = await self._call("remove critic", prefs=prefs)
        assert "removed" in text

    async def test_remove_not_found(self):
        prefs = AsyncMock()
        prefs.remove_persona = AsyncMock(return_value=False)
        text = await self._call("remove nope", prefs=prefs)
        assert "not found" in text

    async def test_show_missing_name(self):
        prefs = AsyncMock()
        text = await self._call("show", prefs=prefs)
        assert "Usage" in text

    async def test_show_found(self):
        prefs = AsyncMock()
        prefs.get_persona = AsyncMock(return_value=FakePersona("critic", "Be critical"))
        text = await self._call("show critic", prefs=prefs)
        assert "**critic**" in text
        assert "Be critical" in text

    async def test_show_not_found(self):
        prefs = AsyncMock()
        prefs.get_persona = AsyncMock(return_value=None)
        text = await self._call("show nope", prefs=prefs)
        assert "not found" in text


# ---------------------------------------------------------------------------
# !memory
# ---------------------------------------------------------------------------


class TestHandleMemory:
    async def _call(
        self, args: str, project: str | None = "proj", facade: Any = None, engine: str | None = None,
    ) -> str:
        send = _make_send()
        await handle_memory(
            args, project=project, facade=facade,
            current_engine=engine, send=send,
        )
        return _sent_text(send)

    async def test_no_project(self):
        text = await self._call("", project=None)
        assert "프로젝트를 먼저" in text

    async def test_no_facade(self):
        text = await self._call("", project="proj", facade=None)
        assert "unavailable" in text

    async def test_summary_empty(self):
        facade = MagicMock()
        facade.memory.get_context_summary = AsyncMock(return_value="")
        text = await self._call("", facade=facade)
        assert "메모리가 없습니다" in text

    async def test_summary_with_content(self):
        facade = MagicMock()
        facade.memory.get_context_summary = AsyncMock(return_value="some summary")
        text = await self._call("", facade=facade)
        assert text == "some summary"

    async def test_list_entries(self):
        facade = MagicMock()
        facade.memory.list_entries = AsyncMock(return_value=[
            FakeEntry(id="abc123def456ghij", type="decision", title="Use pytest"),
        ])
        text = await self._call("list", facade=facade)
        assert "Memory — proj" in text
        assert "decision" in text
        assert "Use pytest" in text

    async def test_list_empty(self):
        facade = MagicMock()
        facade.memory.list_entries = AsyncMock(return_value=[])
        text = await self._call("list", facade=facade)
        assert "No entries" in text

    async def test_list_invalid_type(self):
        facade = MagicMock()
        text = await self._call("list invalid", facade=facade)
        assert "Unknown type" in text

    async def test_add_missing_args(self):
        facade = MagicMock()
        text = await self._call("add decision", facade=facade)
        assert "Usage" in text

    async def test_add_invalid_type(self):
        facade = MagicMock()
        text = await self._call("add bogus title content", facade=facade)
        assert "Unknown type" in text

    async def test_add_success(self):
        facade = MagicMock()
        facade.memory.add_entry = AsyncMock(return_value=FakeEntry(
            id="newid12345678", type="decision", title="Use ruff",
        ))
        text = await self._call("add decision Use-ruff Because-it-is-fast", facade=facade, engine="claude")
        assert "Entry added" in text
        assert "decision" in text
        facade.memory.add_entry.assert_awaited_once()

    async def test_add_uses_default_source(self):
        facade = MagicMock()
        facade.memory.add_entry = AsyncMock(return_value=FakeEntry(
            id="newid12345678", type="idea", title="Test",
        ))
        text = await self._call("add idea Test Content", facade=facade, engine=None)
        call_kwargs = facade.memory.add_entry.call_args.kwargs
        assert call_kwargs["source"] == "user"

    async def test_search_missing_query(self):
        facade = MagicMock()
        text = await self._call("search", facade=facade)
        assert "Usage" in text

    async def test_search_no_results(self):
        facade = MagicMock()
        facade.memory.search = AsyncMock(return_value=[])
        text = await self._call("search foo", facade=facade)
        assert "No results" in text

    async def test_search_with_results(self):
        facade = MagicMock()
        facade.memory.search = AsyncMock(return_value=[
            FakeEntry(id="abc123def456ghij", type="idea", title="Cool idea"),
        ])
        text = await self._call("search cool", facade=facade)
        assert "Search results" in text
        assert "Cool idea" in text

    async def test_delete_missing_id(self):
        facade = MagicMock()
        text = await self._call("delete", facade=facade)
        assert "Usage" in text

    async def test_delete_prefix_too_short(self):
        facade = MagicMock()
        text = await self._call("delete abc", facade=facade)
        assert "too short" in text

    async def test_unknown_subcmd(self):
        facade = MagicMock()
        text = await self._call("bogus", facade=facade)
        assert "Usage" in text


# ---------------------------------------------------------------------------
# !branch
# ---------------------------------------------------------------------------


class TestHandleBranch:
    async def _call(
        self, args: str, project: str | None = "proj", facade: Any = None,
    ) -> str:
        send = _make_send()
        await handle_branch(args, project=project, facade=facade, send=send)
        return _sent_text(send)

    async def test_no_project(self):
        text = await self._call("", project=None)
        assert "프로젝트를 먼저" in text

    async def test_no_facade(self):
        text = await self._call("", project="proj", facade=None)
        assert "unavailable" in text

    async def test_active_branches_empty(self):
        facade = MagicMock()
        facade.conv_branches.list = AsyncMock(return_value=[])
        text = await self._call("", facade=facade)
        assert "활성 대화 분기가 없습니다" in text

    async def test_active_branches(self):
        facade = MagicMock()
        facade.conv_branches.list = AsyncMock(return_value=[
            FakeBranch(branch_id="br12345678901234", label="experiment"),
        ])
        text = await self._call("", facade=facade)
        assert "Active branches" in text
        assert "experiment" in text

    async def test_create_missing_label(self):
        facade = MagicMock()
        text = await self._call("create", facade=facade)
        assert "Usage" in text

    async def test_create_success(self):
        facade = MagicMock()
        facade.conv_branches.create = AsyncMock(
            return_value=FakeBranch(branch_id="newbranch12345678", label="my-branch"),
        )
        text = await self._call("create my-branch", facade=facade)
        assert "Branch created" in text
        assert "my-branch" in text

    async def test_list_invalid_status(self):
        facade = MagicMock()
        text = await self._call("list invalid", facade=facade)
        assert "Unknown status" in text

    async def test_list_empty(self):
        facade = MagicMock()
        facade.conv_branches.list = AsyncMock(return_value=[])
        text = await self._call("list active", facade=facade)
        assert "No branches" in text

    async def test_merge_missing_id(self):
        facade = MagicMock()
        text = await self._call("merge", facade=facade)
        assert "Usage" in text

    async def test_discard_missing_id(self):
        facade = MagicMock()
        text = await self._call("discard", facade=facade)
        assert "Usage" in text

    async def test_unknown_subcmd(self):
        facade = MagicMock()
        text = await self._call("bogus", facade=facade)
        assert "Usage" in text


# ---------------------------------------------------------------------------
# !review
# ---------------------------------------------------------------------------


class TestHandleReview:
    async def _call(
        self, args: str, project: str | None = "proj", facade: Any = None,
    ) -> str:
        send = _make_send()
        await handle_review(args, project=project, facade=facade, send=send)
        return _sent_text(send)

    async def test_no_project(self):
        text = await self._call("", project=None)
        assert "프로젝트를 먼저" in text

    async def test_no_facade(self):
        text = await self._call("", project="proj", facade=None)
        assert "unavailable" in text

    async def test_pending_empty(self):
        facade = MagicMock()
        facade.reviews.list = AsyncMock(return_value=[])
        text = await self._call("", facade=facade)
        assert "대기 중인 리뷰가 없습니다" in text

    async def test_pending_reviews(self):
        facade = MagicMock()
        facade.reviews.list = AsyncMock(return_value=[
            FakeReview(review_id="rev1234567890abcd", artifact_id="art1234567890abcd"),
        ])
        text = await self._call("", facade=facade)
        assert "Pending reviews" in text

    async def test_list_invalid_status(self):
        facade = MagicMock()
        text = await self._call("list invalid", facade=facade)
        assert "Unknown status" in text

    async def test_list_empty(self):
        facade = MagicMock()
        facade.reviews.list = AsyncMock(return_value=[])
        text = await self._call("list pending", facade=facade)
        assert "No reviews" in text

    async def test_approve_missing_id(self):
        facade = MagicMock()
        text = await self._call("approve", facade=facade)
        assert "Usage" in text

    async def test_reject_missing_id(self):
        facade = MagicMock()
        text = await self._call("reject", facade=facade)
        assert "Usage" in text

    async def test_unknown_subcmd(self):
        facade = MagicMock()
        text = await self._call("bogus", facade=facade)
        assert "Usage" in text


# ---------------------------------------------------------------------------
# !context
# ---------------------------------------------------------------------------


class TestHandleContext:
    async def test_no_project(self):
        send = _make_send()
        await handle_context(project=None, facade=None, send=send)
        assert "프로젝트를 먼저" in _sent_text(send)

    async def test_no_facade(self):
        send = _make_send()
        await handle_context(project="proj", facade=None, send=send)
        assert "unavailable" in _sent_text(send)

    async def test_empty_context(self):
        send = _make_send()
        facade = MagicMock()
        facade.get_project_context = AsyncMock(return_value="")
        await handle_context(project="proj", facade=facade, send=send)
        assert "컨텍스트가 없습니다" in _sent_text(send)

    async def test_with_context(self):
        send = _make_send()
        facade = MagicMock()
        facade.get_project_context = AsyncMock(return_value="full context here")
        await handle_context(project="proj", facade=facade, send=send)
        assert _sent_text(send) == "full context here"


# ---------------------------------------------------------------------------
# !rt
# ---------------------------------------------------------------------------


class TestHandleRt:
    async def test_placeholder_response(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude", "codex"])
        runtime.roundtable.engines = ["claude", "codex"]
        await handle_rt("some topic", runtime=runtime, send=send)
        text = _sent_text(send)
        assert "Roundtable" in text
        assert "`claude`" in text

    async def test_fallback_engines(self):
        send = _make_send()
        runtime = _make_runtime(engines=["gemini"])
        runtime.roundtable.engines = []
        await handle_rt("", runtime=runtime, send=send)
        text = _sent_text(send)
        assert "`gemini`" in text


# ---------------------------------------------------------------------------
# _resolve_id
# ---------------------------------------------------------------------------


class TestResolveId:
    async def test_prefix_too_short(self):
        result_id, err = await _resolve_id(
            "abc",
            fetch_all=AsyncMock(return_value=[]),
            get_id=lambda x: x,
            get_label=lambda x: x,
        )
        assert result_id is None
        assert "too short" in err

    async def test_exact_match(self):
        items = ["abcdef123456"]
        result_id, err = await _resolve_id(
            "abcdef123456",
            fetch_all=AsyncMock(return_value=items),
            get_id=lambda x: x,
            get_label=lambda x: x,
        )
        assert result_id == "abcdef123456"
        assert err is None

    async def test_unique_prefix_match(self):
        items = ["abcdef123456", "xyz789000000"]
        result_id, err = await _resolve_id(
            "abcdef",
            fetch_all=AsyncMock(return_value=items),
            get_id=lambda x: x,
            get_label=lambda x: x,
        )
        assert result_id == "abcdef123456"
        assert err is None

    async def test_no_match(self):
        items = ["abcdef123456"]
        result_id, err = await _resolve_id(
            "zzzzzzz",
            fetch_all=AsyncMock(return_value=items),
            get_id=lambda x: x,
            get_label=lambda x: x,
        )
        assert result_id is None
        assert "not found" in err

    async def test_ambiguous_match(self):
        items = ["abcdef111111", "abcdef222222", "abcdef333333"]
        result_id, err = await _resolve_id(
            "abcdef",
            fetch_all=AsyncMock(return_value=items),
            get_id=lambda x: x,
            get_label=lambda x: x,
        )
        assert result_id is None
        assert "Ambiguous" in err
        assert "3 matches" in err

    async def test_min_prefix_len_constant(self):
        assert _MIN_PREFIX_LEN == 6


# ---------------------------------------------------------------------------
# dispatch_command
# ---------------------------------------------------------------------------


class TestDispatchCommand:
    def _defaults(self, **overrides: Any) -> dict[str, Any]:
        d = dict(
            channel_id="ch1",
            runtime=_make_runtime(),
            chat_prefs=None,
            facade=None,
            journal=None,
            context_store=None,
            conv_sessions=None,
            running_tasks={},
            projects_root=None,
            config_path=None,
            send=_make_send(),
        )
        d.update(overrides)
        return d

    async def test_unknown_command_returns_false(self):
        kw = self._defaults()
        result = await dispatch_command("notacommand", "", **kw)
        assert result is False

    async def test_help_returns_true(self):
        kw = self._defaults()
        result = await dispatch_command("help", "", **kw)
        assert result is True

    async def test_new_clears_journal_and_sessions(self):
        journal = AsyncMock()
        conv_sessions = AsyncMock()
        send = _make_send()
        kw = self._defaults(journal=journal, conv_sessions=conv_sessions, send=send)
        result = await dispatch_command("new", "", **kw)
        assert result is True
        journal.mark_reset.assert_awaited_once_with("ch1")
        conv_sessions.clear.assert_awaited_once_with("ch1")
        assert "새 대화" in _sent_text(send)

    async def test_new_without_journal(self):
        send = _make_send()
        kw = self._defaults(send=send)
        result = await dispatch_command("new", "", **kw)
        assert result is True
        assert "새 대화" in _sent_text(send)

    async def test_cancel_no_task(self):
        send = _make_send()
        kw = self._defaults(send=send, running_tasks={})
        result = await dispatch_command("cancel", "", **kw)
        assert result is True
        assert "No running task" in _sent_text(send)

    async def test_cancel_with_matching_task(self):
        from tunapi.transport import MessageRef

        ref = MessageRef(channel_id="ch1", message_id=1)
        cancel_event = MagicMock()
        task = MagicMock()
        task.cancel_requested = cancel_event
        send = _make_send()
        kw = self._defaults(send=send, running_tasks={ref: task})
        result = await dispatch_command("cancel", "", **kw)
        assert result is True
        cancel_event.set.assert_called_once()
        assert "cancelled" in _sent_text(send)

    async def test_cancel_different_channel(self):
        from tunapi.transport import MessageRef

        ref = MessageRef(channel_id="other_ch", message_id=1)
        task = MagicMock()
        task.cancel_requested = MagicMock()
        send = _make_send()
        kw = self._defaults(send=send, running_tasks={ref: task})
        result = await dispatch_command("cancel", "", **kw)
        assert result is True
        assert "No running task" in _sent_text(send)

    async def test_dispatches_model(self):
        send = _make_send()
        kw = self._defaults(send=send)
        result = await dispatch_command("model", "", **kw)
        assert result is True
        assert "Current engine" in _sent_text(send)

    async def test_dispatches_trigger(self):
        send = _make_send()
        kw = self._defaults(send=send)
        result = await dispatch_command("trigger", "all", **kw)
        assert result is True
        assert "respond to all" in _sent_text(send)

    async def test_dispatches_status(self):
        send = _make_send()
        kw = self._defaults(send=send)
        result = await dispatch_command("status", "", **kw)
        assert result is True
        assert "Session status" in _sent_text(send)

    async def test_dispatches_persona(self):
        send = _make_send()
        kw = self._defaults(send=send)
        result = await dispatch_command("persona", "", **kw)
        assert result is True

    async def test_dispatches_memory_no_project(self):
        send = _make_send()
        kw = self._defaults(send=send)
        result = await dispatch_command("memory", "", **kw)
        assert result is True
        assert "프로젝트를 먼저" in _sent_text(send)

    async def test_dispatches_branch(self):
        send = _make_send()
        kw = self._defaults(send=send)
        result = await dispatch_command("branch", "", **kw)
        assert result is True

    async def test_dispatches_review(self):
        send = _make_send()
        kw = self._defaults(send=send)
        result = await dispatch_command("review", "", **kw)
        assert result is True

    async def test_dispatches_context(self):
        send = _make_send()
        kw = self._defaults(send=send)
        result = await dispatch_command("context", "", **kw)
        assert result is True

    async def test_dispatches_rt(self):
        send = _make_send()
        kw = self._defaults(send=send)
        result = await dispatch_command("rt", "", **kw)
        assert result is True

    async def test_dispatches_project(self):
        send = _make_send()
        kw = self._defaults(send=send)
        result = await dispatch_command("project", "list", **kw)
        assert result is True

    async def test_dispatches_models(self):
        send = _make_send()
        kw = self._defaults(send=send)
        result = await dispatch_command("models", "", **kw)
        assert result is True
