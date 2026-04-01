"""Comprehensive tests for Mattermost command handlers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from tunapi.context import RunContext
from tunapi.mattermost.commands import (
    _resolve_id,
    handle_branch,
    handle_cancel,
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
    parse_slash_command,
)
from tunapi.transport import RenderedMessage


# ---------------------------------------------------------------------------
# Helpers & Fakes
# ---------------------------------------------------------------------------


class FakeRuntime:
    default_engine = "claude"

    def __init__(
        self,
        engines: list[str] | None = None,
        projects: list[str] | None = None,
    ):
        self._engines = engines or ["claude", "gemini"]
        self._projects = projects or []

    def available_engine_ids(self):
        return self._engines

    def project_aliases(self):
        return self._projects

    def normalize_project_key(self, name: str) -> str | None:
        for p in self._projects:
            if p.lower() == name.lower():
                return p
        return None


class FakeChatPrefs:
    """In-memory ChatPrefsStore fake with the same async API."""

    def __init__(self):
        self._engines: dict[str, str] = {}
        self._triggers: dict[str, str] = {}
        self._contexts: dict[str, RunContext] = {}
        self._engine_models: dict[str, dict[str, str]] = {}
        self._personas: dict[str, Any] = {}

    async def get_default_engine(self, channel_id: str) -> str | None:
        return self._engines.get(channel_id)

    async def set_default_engine(self, channel_id: str, engine: str, **kw) -> None:
        self._engines[channel_id] = engine

    async def get_trigger_mode(self, channel_id: str) -> str | None:
        return self._triggers.get(channel_id)

    async def set_trigger_mode(self, channel_id: str, mode: str) -> None:
        self._triggers[channel_id] = mode

    async def get_context(self, channel_id: str) -> RunContext | None:
        return self._contexts.get(channel_id)

    async def set_context(self, channel_id: str, ctx: RunContext) -> None:
        self._contexts[channel_id] = ctx

    async def get_engine_model(self, channel_id: str, engine: str) -> str | None:
        return self._engine_models.get(channel_id, {}).get(engine)

    async def set_engine_model(self, channel_id: str, engine: str, model: str) -> None:
        self._engine_models.setdefault(channel_id, {})[engine] = model

    async def clear_engine_model(self, channel_id: str, engine: str) -> None:
        self._engine_models.get(channel_id, {}).pop(engine, None)

    async def get_all_engine_models(self, channel_id: str) -> dict[str, str]:
        return dict(self._engine_models.get(channel_id, {}))

    # Persona
    async def add_persona(self, name: str, prompt: str) -> None:
        self._personas[name] = _FakePersona(name=name, prompt=prompt)

    async def list_personas(self) -> dict[str, Any]:
        return dict(self._personas)

    async def get_persona(self, name: str) -> Any | None:
        return self._personas.get(name)

    async def remove_persona(self, name: str) -> bool:
        return self._personas.pop(name, None) is not None


@dataclass
class _FakePersona:
    name: str
    prompt: str


def _captured_send() -> tuple[AsyncMock, list[str]]:
    """Return (send_fn, captured_texts)."""
    texts: list[str] = []

    async def _send(msg: RenderedMessage) -> None:
        texts.append(msg.text)

    return AsyncMock(side_effect=_send), texts


CH = "ch-001"


# ---------------------------------------------------------------------------
# parse_slash_command (alias for core parse_command)
# ---------------------------------------------------------------------------


class TestParseSlashCommand:
    def test_bang_command(self):
        cmd, args = parse_slash_command("!help")
        assert cmd == "help"
        assert args == ""

    def test_bang_command_with_args(self):
        cmd, args = parse_slash_command("!model claude opus")
        assert cmd == "model"
        assert args == "claude opus"

    def test_slash_prefix_not_parsed(self):
        cmd, _ = parse_slash_command("/help")
        assert cmd is None

    def test_plain_text(self):
        cmd, _ = parse_slash_command("hello world")
        assert cmd is None

    def test_empty(self):
        cmd, _ = parse_slash_command("")
        assert cmd is None


# ---------------------------------------------------------------------------
# handle_help
# ---------------------------------------------------------------------------


class TestHandleHelp:
    def test_help_contains_all_commands(self):
        send, texts = _captured_send()
        anyio.run(lambda: handle_help(runtime=FakeRuntime(), send=send))
        assert texts
        help_text = texts[0]
        for cmd in ("help", "new", "model", "models", "trigger", "project",
                     "persona", "memory", "branch", "review", "context",
                     "rt", "file", "status", "cancel"):
            assert f"!{cmd}" in help_text, f"!{cmd} missing from help"

    def test_help_lists_engines(self):
        send, texts = _captured_send()
        rt = FakeRuntime(engines=["claude", "gemini"])
        anyio.run(lambda: handle_help(runtime=rt, send=send))
        assert "`claude`" in texts[0]
        assert "`gemini`" in texts[0]

    def test_help_lists_projects(self):
        send, texts = _captured_send()
        rt = FakeRuntime(projects=["myproj"])
        anyio.run(lambda: handle_help(runtime=rt, send=send))
        assert "`myproj`" in texts[0]

    def test_help_no_engines(self):
        send, texts = _captured_send()
        rt = FakeRuntime(engines=[])
        anyio.run(lambda: handle_help(runtime=rt, send=send))
        assert "none" in texts[0]


# ---------------------------------------------------------------------------
# handle_model
# ---------------------------------------------------------------------------


class TestHandleModel:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.prefs = FakeChatPrefs()
        self.rt = FakeRuntime()

    def _run(self, args: str):
        anyio.run(lambda: handle_model(
            args, channel_id=CH, runtime=self.rt,
            chat_prefs=self.prefs, send=self.send,
        ))

    def test_no_args_shows_current(self, setup):
        self._run("")
        assert "Current engine" in self.texts[0]
        assert "`claude`" in self.texts[0]

    def test_no_args_with_model_override(self, setup):
        anyio.run(lambda: self.prefs.set_engine_model(CH, "claude", "opus-4"))
        self._run("")
        assert "opus-4" in self.texts[0]

    def test_switch_engine(self, setup):
        self._run("gemini")
        assert "gemini" in self.texts[0]
        assert anyio.run(lambda: self.prefs.get_default_engine(CH)) == "gemini"

    def test_unknown_engine(self, setup):
        self._run("nonexistent")
        assert "Unknown engine" in self.texts[0]

    def test_set_model(self, setup):
        self._run("claude my-model")
        assert "my-model" in self.texts[0]
        assert anyio.run(lambda: self.prefs.get_engine_model(CH, "claude")) == "my-model"

    def test_clear_model(self, setup):
        anyio.run(lambda: self.prefs.set_engine_model(CH, "claude", "old"))
        self._run("claude clear")
        assert "cleared" in self.texts[0].lower()
        assert anyio.run(lambda: self.prefs.get_engine_model(CH, "claude")) is None

    def test_case_insensitive_engine(self, setup):
        self._run("Claude")
        assert "claude" in self.texts[0].lower()

    def test_no_chat_prefs(self, setup):
        """Should not crash when chat_prefs is None."""
        anyio.run(lambda: handle_model(
            "", channel_id=CH, runtime=self.rt,
            chat_prefs=None, send=self.send,
        ))
        assert "Current engine" in self.texts[0]


# ---------------------------------------------------------------------------
# handle_models
# ---------------------------------------------------------------------------


class TestHandleModels:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.prefs = FakeChatPrefs()
        self.rt = FakeRuntime()

    def _run(self, args: str):
        anyio.run(lambda: handle_models(
            args, channel_id=CH, runtime=self.rt,
            chat_prefs=self.prefs, send=self.send,
        ))

    def test_all_engines(self, setup):
        self._run("")
        assert "Available Models" in self.texts[0]

    def test_specific_engine(self, setup):
        self._run("claude")
        assert "claude" in self.texts[0].lower()

    def test_unknown_engine(self, setup):
        self._run("unknown")
        assert "Unknown engine" in self.texts[0]


# ---------------------------------------------------------------------------
# handle_trigger
# ---------------------------------------------------------------------------


class TestHandleTrigger:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.prefs = FakeChatPrefs()

    def _run(self, args: str):
        anyio.run(lambda: handle_trigger(
            args, channel_id=CH, chat_prefs=self.prefs, send=self.send,
        ))

    def test_set_all(self, setup):
        self._run("all")
        assert "all" in self.texts[0]
        assert anyio.run(lambda: self.prefs.get_trigger_mode(CH)) == "all"

    def test_set_mentions(self, setup):
        self._run("mentions")
        assert "mentions" in self.texts[0]
        assert anyio.run(lambda: self.prefs.get_trigger_mode(CH)) == "mentions"

    def test_invalid_mode_shows_usage(self, setup):
        self._run("invalid")
        assert "Usage" in self.texts[0] or "Current trigger" in self.texts[0]

    def test_no_args_shows_current(self, setup):
        self._run("")
        assert "Current trigger mode" in self.texts[0]

    def test_no_chat_prefs(self, setup):
        anyio.run(lambda: handle_trigger(
            "all", channel_id=CH, chat_prefs=None, send=self.send,
        ))
        assert "all" in self.texts[0]


# ---------------------------------------------------------------------------
# handle_status
# ---------------------------------------------------------------------------


class TestHandleStatus:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.prefs = FakeChatPrefs()
        self.rt = FakeRuntime()

    def _run(self, *, has_session: bool = False, session_engine: str | None = None):
        anyio.run(lambda: handle_status(
            channel_id=CH, runtime=self.rt, chat_prefs=self.prefs,
            session_engine=session_engine, has_session=has_session, send=self.send,
        ))

    def test_default_status(self, setup):
        self._run()
        text = self.texts[0]
        assert "Session status" in text
        assert "claude" in text
        assert "none" in text.lower()

    def test_active_session(self, setup):
        self._run(has_session=True)
        assert "active" in self.texts[0]

    def test_project_bound(self, setup):
        anyio.run(lambda: self.prefs.set_context(CH, RunContext(project="myproj", branch="feat-x")))
        self._run()
        assert "myproj" in self.texts[0]
        assert "feat-x" in self.texts[0]

    def test_no_chat_prefs(self, setup):
        anyio.run(lambda: handle_status(
            channel_id=CH, runtime=self.rt, chat_prefs=None,
            session_engine=None, has_session=False, send=self.send,
        ))
        assert "Session status" in self.texts[0]


# ---------------------------------------------------------------------------
# handle_project
# ---------------------------------------------------------------------------


class TestHandleProject:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.prefs = FakeChatPrefs()
        self.rt = FakeRuntime(projects=["backend", "frontend"])

    def _run(self, args: str):
        anyio.run(lambda: handle_project(
            args, channel_id=CH, runtime=self.rt,
            chat_prefs=self.prefs, projects_root=None, send=self.send,
        ))

    def test_default_shows_usage(self, setup):
        self._run("")
        assert "Usage" in self.texts[0]

    def test_list(self, setup):
        self._run("list")
        assert "backend" in self.texts[0]
        assert "frontend" in self.texts[0]

    def test_set_known(self, setup):
        self._run("set backend")
        assert "backend" in self.texts[0]
        ctx = anyio.run(lambda: self.prefs.get_context(CH))
        assert ctx is not None
        assert ctx.project == "backend"

    def test_set_unknown(self, setup):
        self._run("set nonexistent")
        assert "Unknown project" in self.texts[0]

    def test_set_no_args(self, setup):
        self._run("set")
        assert "Usage" in self.texts[0]

    def test_info_bound(self, setup):
        anyio.run(lambda: self.prefs.set_context(CH, RunContext(project="backend", branch="main")))
        self._run("info")
        assert "backend" in self.texts[0]
        assert "main" in self.texts[0]

    def test_info_unbound(self, setup):
        self._run("info")
        assert "No project bound" in self.texts[0]

    def test_list_no_projects(self, setup):
        self.rt = FakeRuntime(projects=[])
        anyio.run(lambda: handle_project(
            "list", channel_id=CH, runtime=self.rt,
            chat_prefs=self.prefs, projects_root=None, send=self.send,
        ))
        assert "No projects found" in self.texts[0]

    def test_discovered_project(self, setup, tmp_path):
        """Discovered projects from projects_root with .git dirs."""
        proj_dir = tmp_path / "discovered"
        proj_dir.mkdir()
        (proj_dir / ".git").mkdir()
        rt = FakeRuntime(projects=[])
        anyio.run(lambda: handle_project(
            "list", channel_id=CH, runtime=rt,
            chat_prefs=self.prefs, projects_root=str(tmp_path), send=self.send,
        ))
        assert "discovered" in self.texts[0]


# ---------------------------------------------------------------------------
# handle_persona
# ---------------------------------------------------------------------------


class TestHandlePersona:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.prefs = FakeChatPrefs()

    def _run(self, args: str):
        anyio.run(lambda: handle_persona(
            args, chat_prefs=self.prefs, send=self.send,
        ))

    def test_no_prefs(self, setup):
        anyio.run(lambda: handle_persona(
            "", chat_prefs=None, send=self.send,
        ))
        assert "unavailable" in self.texts[0].lower()

    def test_add_persona(self, setup):
        self._run('add reviewer "Be critical"')
        assert "added" in self.texts[0].lower()

    def test_add_no_prompt(self, setup):
        self._run("add reviewer")
        assert "Usage" in self.texts[0]

    def test_list_empty(self, setup):
        self._run("list")
        assert "No personas" in self.texts[0]

    def test_list_with_personas(self, setup):
        anyio.run(lambda: self.prefs.add_persona("test", "prompt"))
        self._run("list")
        assert "test" in self.texts[0]

    def test_show_existing(self, setup):
        anyio.run(lambda: self.prefs.add_persona("critic", "Be harsh"))
        self._run("show critic")
        assert "Be harsh" in self.texts[0]

    def test_show_missing(self, setup):
        self._run("show unknown")
        assert "not found" in self.texts[0].lower()

    def test_show_no_name(self, setup):
        self._run("show")
        assert "Usage" in self.texts[0]

    def test_remove_existing(self, setup):
        anyio.run(lambda: self.prefs.add_persona("old", "x"))
        self._run("remove old")
        assert "removed" in self.texts[0].lower()

    def test_remove_missing(self, setup):
        self._run("remove nope")
        assert "not found" in self.texts[0].lower()

    def test_remove_no_name(self, setup):
        self._run("remove")
        assert "Usage" in self.texts[0]

    def test_default_usage(self, setup):
        self._run("unknown")
        assert "Usage" in self.texts[0]

    def test_add_empty_prompt(self, setup):
        self._run("add name")
        assert "Usage" in self.texts[0]


# ---------------------------------------------------------------------------
# handle_rt
# ---------------------------------------------------------------------------


class TestHandleRt:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()

        class _RTConfig:
            engines = ["claude", "gemini"]
            rounds = 1
            max_rounds = 5

        self.rt = FakeRuntime()
        self.rt.roundtable = _RTConfig()
        self.start_rt = AsyncMock()

    def _run(self, args: str, **kw):
        anyio.run(lambda: handle_rt(
            args, runtime=self.rt, send=self.send,
            start_roundtable=self.start_rt, **kw,
        ))

    def test_no_engines(self, setup):
        self.rt.roundtable.engines = []
        self.rt._engines = []
        self._run('"topic"')
        assert "No engines" in self.texts[0]

    def test_no_topic_shows_usage(self, setup):
        self._run("")
        assert "Roundtable" in self.texts[0]
        assert "Usage" in self.texts[0]

    def test_start_roundtable(self, setup):
        self._run('"my topic"')
        self.start_rt.assert_awaited_once()
        call_args = self.start_rt.call_args
        assert call_args[0][0] == "my topic"

    def test_close_no_handler(self, setup):
        self._run("close")
        # Should show error since close_roundtable is None
        assert len(self.texts) == 1

    def test_close_with_handler(self, setup):
        close_fn = AsyncMock()
        self._run("close", close_roundtable=close_fn)
        close_fn.assert_awaited_once()

    def test_follow_no_handler(self, setup):
        self._run('follow "question"')
        assert len(self.texts) == 1

    def test_follow_no_topic(self, setup):
        cont = AsyncMock()
        self._run("follow", continue_roundtable=cont)
        assert "Usage" in self.texts[0] or "Follow-up" in self.texts[0]

    def test_follow_with_topic(self, setup):
        cont = AsyncMock()
        self._run('follow "follow-up question"', continue_roundtable=cont)
        cont.assert_awaited_once()


# ---------------------------------------------------------------------------
# handle_memory
# ---------------------------------------------------------------------------


@dataclass
class _FakeEntry:
    id: str
    type: str
    title: str
    content: str
    tags: list[str]
    timestamp: float
    source: str = "user"


class FakeMemory:
    def __init__(self):
        self._entries: list[_FakeEntry] = []

    async def get_context_summary(self, project: str, max_per_type: int = 5) -> str:
        if not self._entries:
            return ""
        return "Summary of entries"

    async def list_entries(self, project: str, type: str | None = None, limit: int = 20) -> list[_FakeEntry]:
        entries = self._entries
        if type:
            entries = [e for e in entries if e.type == type]
        return entries[:limit]

    async def add_entry(self, project: str, *, type: str, title: str, content: str, source: str) -> _FakeEntry:
        import uuid
        e = _FakeEntry(
            id=uuid.uuid4().hex, type=type, title=title,
            content=content, tags=[], timestamp=1000.0, source=source,
        )
        self._entries.append(e)
        return e

    async def search(self, project: str, query: str) -> list[_FakeEntry]:
        return [e for e in self._entries if query.lower() in e.title.lower()]

    async def delete_entry(self, project: str, entry_id: str) -> bool:
        for i, e in enumerate(self._entries):
            if e.id == entry_id:
                self._entries.pop(i)
                return True
        return False


class FakeFacade:
    def __init__(self):
        self.memory = FakeMemory()


class TestHandleMemory:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.facade = FakeFacade()

    def _run(self, args: str, project: str | None = "proj"):
        anyio.run(lambda: handle_memory(
            args, project=project, facade=self.facade, send=self.send,
        ))

    def test_no_project(self, setup):
        self._run("", project=None)
        assert "프로젝트를 먼저" in self.texts[0]

    def test_no_facade(self, setup):
        anyio.run(lambda: handle_memory(
            "", project="p", facade=None, send=self.send,
        ))
        assert "unavailable" in self.texts[0].lower()

    def test_summary_empty(self, setup):
        self._run("")
        assert "메모리가 없습니다" in self.texts[0]

    def test_summary_with_entries(self, setup):
        anyio.run(lambda: self.facade.memory.add_entry(
            "proj", type="decision", title="T", content="C", source="user",
        ))
        self._run("")
        assert "Summary" in self.texts[0]

    def test_list_empty(self, setup):
        self._run("list")
        assert "No entries" in self.texts[0]

    def test_list_with_type_filter(self, setup):
        anyio.run(lambda: self.facade.memory.add_entry(
            "proj", type="decision", title="D", content="C", source="user",
        ))
        self._run("list decision")
        assert "decision" in self.texts[0].lower()

    def test_list_invalid_type(self, setup):
        self._run("list invalid")
        assert "Unknown type" in self.texts[0]

    def test_add_entry(self, setup):
        self._run("add decision Title Content here")
        assert "added" in self.texts[0].lower()

    def test_add_invalid_type(self, setup):
        self._run("add badtype Title Content")
        assert "Unknown type" in self.texts[0]

    def test_add_insufficient_args(self, setup):
        self._run("add decision")
        assert "Usage" in self.texts[0]

    def test_search_no_query(self, setup):
        self._run("search")
        assert "Usage" in self.texts[0]

    def test_search_no_results(self, setup):
        self._run("search nonexistent")
        assert "No results" in self.texts[0]

    def test_search_with_results(self, setup):
        anyio.run(lambda: self.facade.memory.add_entry(
            "proj", type="idea", title="widget", content="C", source="user",
        ))
        self._run("search widget")
        assert "widget" in self.texts[0]

    def test_delete_no_args(self, setup):
        self._run("delete")
        assert "Usage" in self.texts[0]

    def test_unknown_subcmd_shows_usage(self, setup):
        self._run("bogus")
        assert "Usage" in self.texts[0]


# ---------------------------------------------------------------------------
# handle_branch
# ---------------------------------------------------------------------------


@dataclass
class _FakeBranch:
    branch_id: str
    label: str
    status: str = "active"
    git_branch: str | None = None


class FakeConvBranches:
    def __init__(self):
        self._branches: list[_FakeBranch] = []

    async def list(self, project: str, status: str | None = None) -> list[_FakeBranch]:
        if status:
            return [b for b in self._branches if b.status == status]
        return list(self._branches)

    async def create(self, project: str, label: str) -> _FakeBranch:
        import uuid
        b = _FakeBranch(branch_id=uuid.uuid4().hex, label=label)
        self._branches.append(b)
        return b

    async def merge(self, project: str, branch_id: str) -> _FakeBranch | None:
        for b in self._branches:
            if b.branch_id == branch_id:
                b.status = "merged"
                return b
        return None

    async def discard(self, project: str, branch_id: str) -> _FakeBranch | None:
        for b in self._branches:
            if b.branch_id == branch_id:
                b.status = "discarded"
                return b
        return None

    async def link_git_branch(self, project: str, branch_id: str, git_branch: str) -> _FakeBranch | None:
        for b in self._branches:
            if b.branch_id == branch_id:
                b.git_branch = git_branch
                return b
        return None


class FakeBranchFacade:
    def __init__(self):
        self.conv_branches = FakeConvBranches()


class TestHandleBranch:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.facade = FakeBranchFacade()

    def _run(self, args: str, project: str | None = "proj"):
        anyio.run(lambda: handle_branch(
            args, project=project, facade=self.facade, send=self.send,
        ))

    def test_no_project(self, setup):
        self._run("", project=None)
        assert "프로젝트를 먼저" in self.texts[0]

    def test_no_facade(self, setup):
        anyio.run(lambda: handle_branch(
            "", project="p", facade=None, send=self.send,
        ))
        assert "unavailable" in self.texts[0].lower()

    def test_default_no_branches(self, setup):
        self._run("")
        assert "활성 대화 분기가 없습니다" in self.texts[0]

    def test_default_with_branches(self, setup):
        anyio.run(lambda: self.facade.conv_branches.create("proj", "feature-x"))
        self._run("")
        assert "feature-x" in self.texts[0]

    def test_create(self, setup):
        self._run("create my-branch")
        assert "created" in self.texts[0].lower()
        assert "my-branch" in self.texts[0]

    def test_create_no_label(self, setup):
        self._run("create")
        assert "Usage" in self.texts[0]

    def test_list_by_status(self, setup):
        self._run("list active")
        # No branches, shows empty message
        assert "No branches" in self.texts[0]

    def test_list_invalid_status(self, setup):
        self._run("list bogus")
        assert "Unknown status" in self.texts[0]

    def test_default_usage(self, setup):
        self._run("unknown-subcmd")
        assert "Usage" in self.texts[0]


# ---------------------------------------------------------------------------
# handle_review
# ---------------------------------------------------------------------------


@dataclass
class _FakeReview:
    review_id: str
    artifact_id: str
    artifact_version: int
    status: str
    created_at: str = "2026-01-01"


class FakeReviews:
    def __init__(self):
        self._reviews: list[_FakeReview] = []

    async def list(self, project: str, status: str | None = None) -> list[_FakeReview]:
        if status:
            return [r for r in self._reviews if r.status == status]
        return list(self._reviews)

    async def approve(self, project: str, review_id: str, comment: str = "") -> _FakeReview | None:
        for r in self._reviews:
            if r.review_id == review_id:
                r.status = "approved"
                return r
        return None

    async def reject(self, project: str, review_id: str, comment: str = "") -> _FakeReview | None:
        for r in self._reviews:
            if r.review_id == review_id:
                r.status = "rejected"
                return r
        return None


class FakeReviewFacade:
    def __init__(self):
        self.reviews = FakeReviews()


class TestHandleReview:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.facade = FakeReviewFacade()

    def _run(self, args: str, project: str | None = "proj"):
        anyio.run(lambda: handle_review(
            args, project=project, facade=self.facade, send=self.send,
        ))

    def test_no_project(self, setup):
        self._run("", project=None)
        assert "프로젝트를 먼저" in self.texts[0]

    def test_no_facade(self, setup):
        anyio.run(lambda: handle_review(
            "", project="p", facade=None, send=self.send,
        ))
        assert "unavailable" in self.texts[0].lower()

    def test_default_no_pending(self, setup):
        self._run("")
        assert "대기 중인 리뷰가 없습니다" in self.texts[0]

    def test_list_invalid_status(self, setup):
        self._run("list bogus")
        assert "Unknown status" in self.texts[0]

    def test_approve_no_args(self, setup):
        self._run("approve")
        assert "Usage" in self.texts[0]

    def test_reject_no_args(self, setup):
        self._run("reject")
        assert "Usage" in self.texts[0]

    def test_default_usage(self, setup):
        self._run("unknown")
        assert "Usage" in self.texts[0]


# ---------------------------------------------------------------------------
# handle_context
# ---------------------------------------------------------------------------


class TestHandleContext:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()

    def test_no_project(self, setup):
        anyio.run(lambda: handle_context(
            project=None, facade=None, send=self.send,
        ))
        assert "프로젝트를 먼저" in self.texts[0]

    def test_no_facade(self, setup):
        anyio.run(lambda: handle_context(
            project="p", facade=None, send=self.send,
        ))
        assert "unavailable" in self.texts[0].lower()

    def test_no_context(self, setup):
        facade = MagicMock()
        facade.get_project_context = AsyncMock(return_value=None)
        anyio.run(lambda: handle_context(
            project="p", facade=facade, send=self.send,
        ))
        assert "컨텍스트가 없습니다" in self.texts[0]

    def test_with_context(self, setup):
        facade = MagicMock()
        facade.get_project_context = AsyncMock(return_value="Project X context")
        anyio.run(lambda: handle_context(
            project="p", facade=facade, send=self.send,
        ))
        assert "Project X context" in self.texts[0]


# ---------------------------------------------------------------------------
# handle_cancel
# ---------------------------------------------------------------------------


class TestHandleCancel:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()

    def test_no_running_tasks(self, setup):
        anyio.run(lambda: handle_cancel(
            channel_id=CH, running_tasks={}, send=self.send,
        ))
        assert "No running task" in self.texts[0]

    def test_cancel_existing(self, setup):
        @dataclass(frozen=True)
        class Ref:
            channel_id: str

        class Task:
            def __init__(self):
                self.cancel_requested = MagicMock()
                self.cancel_requested.set = MagicMock()

        ref = Ref(channel_id=CH)
        task = Task()
        anyio.run(lambda: handle_cancel(
            channel_id=CH, running_tasks={ref: task}, send=self.send,
        ))
        assert "cancelled" in self.texts[0].lower()
        task.cancel_requested.set.assert_called_once()

    def test_cancel_other_channel(self, setup):
        @dataclass(frozen=True)
        class Ref:
            channel_id: str

        class Task:
            cancel_requested = MagicMock()

        ref = Ref(channel_id="other-ch")
        anyio.run(lambda: handle_cancel(
            channel_id=CH, running_tasks={ref: Task()}, send=self.send,
        ))
        assert "No running task" in self.texts[0]


# ---------------------------------------------------------------------------
# _resolve_id
# ---------------------------------------------------------------------------


class TestResolveId:
    def test_prefix_too_short(self):
        result = anyio.run(lambda: _resolve_id(
            "abc",
            fetch_all=AsyncMock(return_value=[]),
            get_id=lambda x: x,
            get_label=lambda x: x,
        ))
        assert result[0] is None
        assert "too short" in result[1]

    def test_exact_match(self):
        full_id = "abcdef1234567890"
        result = anyio.run(lambda: _resolve_id(
            full_id,
            fetch_all=AsyncMock(return_value=[full_id]),
            get_id=lambda x: x,
            get_label=lambda x: x,
        ))
        assert result == (full_id, None)

    def test_prefix_match(self):
        full_id = "abcdef1234567890"
        result = anyio.run(lambda: _resolve_id(
            "abcdef",
            fetch_all=AsyncMock(return_value=[full_id]),
            get_id=lambda x: x,
            get_label=lambda x: x,
        ))
        assert result == (full_id, None)

    def test_no_match(self):
        result = anyio.run(lambda: _resolve_id(
            "xxxxxx",
            fetch_all=AsyncMock(return_value=["abcdef1234"]),
            get_id=lambda x: x,
            get_label=lambda x: x,
        ))
        assert result[0] is None
        assert "not found" in result[1]

    def test_ambiguous(self):
        items = ["abcdef111111", "abcdef222222"]
        result = anyio.run(lambda: _resolve_id(
            "abcdef",
            fetch_all=AsyncMock(return_value=items),
            get_id=lambda x: x,
            get_label=lambda x: "label",
        ))
        assert result[0] is None
        assert "Ambiguous" in result[1]
        assert "2 matches" in result[1]


# ---------------------------------------------------------------------------
# Help-Dispatcher consistency (mirrors Slack test)
# ---------------------------------------------------------------------------

_DISPATCHER_COMMANDS = {
    "new", "help", "model", "models", "trigger", "project",
    "persona", "memory", "branch", "review", "context",
    "rt", "file", "status", "cancel",
}


def _extract_help_commands(help_text: str) -> set[str]:
    commands: set[str] = set()
    for line in help_text.splitlines():
        if line.startswith("|") and "`!" in line:
            matches = re.findall(r"`!(\w+)", line)
            commands.update(matches)
    return commands


class TestHelpDispatcherConsistency:
    def test_all_help_commands_have_dispatchers(self):
        send, texts = _captured_send()
        anyio.run(lambda: handle_help(runtime=FakeRuntime(), send=send))
        help_cmds = _extract_help_commands(texts[0])
        assert help_cmds, "should extract commands from help"
        undispatched = help_cmds - _DISPATCHER_COMMANDS
        assert not undispatched, f"Help lists commands without dispatchers: {undispatched}"

    def test_dispatcher_commands_in_help(self):
        send, texts = _captured_send()
        anyio.run(lambda: handle_help(runtime=FakeRuntime(), send=send))
        help_cmds = _extract_help_commands(texts[0])
        missing = _DISPATCHER_COMMANDS - help_cmds
        assert not missing, f"Dispatched commands missing from help: {missing}"
