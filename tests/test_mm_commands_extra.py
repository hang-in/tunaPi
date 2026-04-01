"""Extra tests for Mattermost command handlers — covers uncovered branches."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from tunapi.context import RunContext
from tunapi.mattermost.commands import (
    _register_project_in_config,
    _resolve_id,
    handle_branch,
    handle_memory,
    handle_model,
    handle_models,
    handle_project,
    handle_review,
    handle_rt,
    handle_trigger,
)
from tunapi.transport import RenderedMessage


# ---------------------------------------------------------------------------
# Helpers & Fakes (shared)
# ---------------------------------------------------------------------------


class FakeRuntime:
    default_engine = "claude"

    def __init__(
        self,
        engines: list[str] | None = None,
        projects: list[str] | None = None,
    ):
        self._engines = engines or ["claude", "gemini"]
        self._projects_list = projects or []
        self._projects = MagicMock()

    def available_engine_ids(self):
        return self._engines

    def project_aliases(self):
        return self._projects_list

    def normalize_project_key(self, name: str) -> str | None:
        for p in self._projects_list:
            if p.lower() == name.lower():
                return p
        return None


class FakeChatPrefs:
    def __init__(self):
        self._engines: dict[str, str] = {}
        self._triggers: dict[str, str] = {}
        self._contexts: dict[str, RunContext] = {}
        self._engine_models: dict[str, dict[str, str]] = {}

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


def _captured_send() -> tuple[AsyncMock, list[str]]:
    texts: list[str] = []

    async def _send(msg: RenderedMessage) -> None:
        texts.append(msg.text)

    return AsyncMock(side_effect=_send), texts


CH = "ch-extra"


# ---------------------------------------------------------------------------
# handle_model — uncovered branches
# ---------------------------------------------------------------------------


class TestHandleModelExtra:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.prefs = FakeChatPrefs()
        self.rt = FakeRuntime()

    def _run(self, args: str):
        anyio.run(
            lambda: handle_model(
                args,
                channel_id=CH,
                runtime=self.rt,
                chat_prefs=self.prefs,
                send=self.send,
            )
        )

    def test_switch_engine_shows_model_when_set(self, setup):
        """When switching engine that already has a model override, show it."""
        anyio.run(lambda: self.prefs.set_engine_model(CH, "gemini", "gemini-2.5-pro"))
        self._run("gemini")
        assert "gemini-2.5-pro" in self.texts[0]
        assert "model:" in self.texts[0].lower()

    def test_set_model_without_chat_prefs(self, setup):
        """Setting model with chat_prefs=None should not crash."""
        anyio.run(
            lambda: handle_model(
                "claude my-model",
                channel_id=CH,
                runtime=self.rt,
                chat_prefs=None,
                send=self.send,
            )
        )
        assert "my-model" in self.texts[0]

    def test_clear_model_without_chat_prefs(self, setup):
        """Clearing model with chat_prefs=None should not crash."""
        anyio.run(
            lambda: handle_model(
                "claude clear",
                channel_id=CH,
                runtime=self.rt,
                chat_prefs=None,
                send=self.send,
            )
        )
        assert "cleared" in self.texts[0].lower()


# ---------------------------------------------------------------------------
# handle_models — uncovered: current model marker
# ---------------------------------------------------------------------------


class TestHandleModelsExtra:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.prefs = FakeChatPrefs()
        self.rt = FakeRuntime()

    def test_shows_current_model_marker(self, setup):
        """When a model override is set, the marker should be visible."""
        anyio.run(lambda: self.prefs.set_engine_model(CH, "claude", "opus-4"))
        anyio.run(
            lambda: handle_models(
                "",
                channel_id=CH,
                runtime=self.rt,
                chat_prefs=self.prefs,
                send=self.send,
            )
        )
        assert "current" in self.texts[0].lower()
        assert "opus-4" in self.texts[0]


# ---------------------------------------------------------------------------
# handle_trigger — uncovered: trigger with existing stored mode
# ---------------------------------------------------------------------------


class TestHandleTriggerExtra:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.prefs = FakeChatPrefs()

    def test_invalid_mode_shows_current_stored(self, setup):
        """Invalid trigger mode shows the current stored mode."""
        anyio.run(lambda: self.prefs.set_trigger_mode(CH, "mentions"))
        anyio.run(
            lambda: handle_trigger(
                "invalid",
                channel_id=CH,
                chat_prefs=self.prefs,
                send=self.send,
            )
        )
        assert "mentions" in self.texts[0]


# ---------------------------------------------------------------------------
# handle_project — uncovered branches: discovered projects, info branches
# ---------------------------------------------------------------------------


class TestHandleProjectExtra:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.prefs = FakeChatPrefs()

    def test_set_discovered_project(self, setup, tmp_path):
        """Discovered project from projects_root gets auto-registered."""
        proj_dir = tmp_path / "newproj"
        proj_dir.mkdir()
        (proj_dir / ".git").mkdir()

        rt = FakeRuntime(projects=[])
        anyio.run(
            lambda: handle_project(
                "set newproj",
                channel_id=CH,
                runtime=rt,
                chat_prefs=self.prefs,
                projects_root=str(tmp_path),
                config_path=tmp_path / "tunapi.toml",
                send=self.send,
            )
        )
        assert "newproj" in self.texts[0]
        # Verify the runtime register call was made
        rt._projects.register_discovered.assert_called_once()

    def test_info_with_project_no_branch(self, setup):
        """Info shows project without branch."""
        anyio.run(lambda: self.prefs.set_context(CH, RunContext(project="myproj")))
        rt = FakeRuntime(projects=["myproj"])
        anyio.run(
            lambda: handle_project(
                "info",
                channel_id=CH,
                runtime=rt,
                chat_prefs=self.prefs,
                projects_root=None,
                send=self.send,
            )
        )
        assert "myproj" in self.texts[0]
        # Should NOT contain "Branch:" since there's no branch
        assert "Branch" not in self.texts[0]

    def test_info_no_chat_prefs(self, setup):
        """Info with no chat_prefs shows 'no project bound'."""
        rt = FakeRuntime()
        anyio.run(
            lambda: handle_project(
                "info",
                channel_id=CH,
                runtime=rt,
                chat_prefs=None,
                projects_root=None,
                send=self.send,
            )
        )
        assert "No project bound" in self.texts[0]


# ---------------------------------------------------------------------------
# _register_project_in_config
# ---------------------------------------------------------------------------


def test_register_project_in_config(tmp_path):
    """Test _register_project_in_config writes to config and calls runtime."""
    import tomli_w, tomllib

    config_path = tmp_path / "tunapi.toml"
    config_path.write_text(tomli_w.dumps({"projects": {}}))

    rt = FakeRuntime()
    proj_dir = tmp_path / "myproj"
    proj_dir.mkdir()

    _register_project_in_config(
        "myproj",
        proj_dir,
        "ch1",
        runtime=rt,
        config_path=config_path,
    )

    reloaded = tomllib.loads(config_path.read_text())
    assert "myproj" in reloaded["projects"]
    rt._projects.register_discovered.assert_called_once()


def test_register_project_in_config_error_ignored(tmp_path):
    """Config write errors should not crash."""
    rt = FakeRuntime()
    proj_dir = tmp_path / "myproj"
    proj_dir.mkdir()

    # Pass non-existent path, should not crash
    _register_project_in_config(
        "myproj",
        proj_dir,
        "ch1",
        runtime=rt,
        config_path=tmp_path / "nonexistent" / "tunapi.toml",
    )
    rt._projects.register_discovered.assert_called_once()


# ---------------------------------------------------------------------------
# handle_rt — uncovered: error from parse_rt_args, follow with error
# ---------------------------------------------------------------------------


class TestHandleRtExtra:
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
        anyio.run(
            lambda: handle_rt(
                args,
                runtime=self.rt,
                send=self.send,
                start_roundtable=self.start_rt,
                **kw,
            )
        )

    def test_follow_with_error_from_parse(self, setup):
        """Follow-up with invalid engines calls continue_roundtable or shows error."""
        cont = AsyncMock()
        self._run('follow unknownengine "topic"', continue_roundtable=cont)
        # parse_followup_args treats unknownengine as part of topic or filters it
        # Either it calls cont or shows an error message
        assert cont.await_count > 0 or len(self.texts) >= 1

    def test_rt_parse_error(self, setup):
        """Invalid --rounds value triggers error."""
        self._run('"topic" --rounds abc')
        assert len(self.texts) == 1


# ---------------------------------------------------------------------------
# handle_memory — uncovered: delete entry
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
        return "" if not self._entries else "Summary"

    async def list_entries(
        self, project: str, type: str | None = None, limit: int = 20
    ) -> list[_FakeEntry]:
        entries = self._entries
        if type:
            entries = [e for e in entries if e.type == type]
        return entries[:limit]

    async def add_entry(
        self, project: str, *, type: str, title: str, content: str, source: str
    ) -> _FakeEntry:
        e = _FakeEntry(
            id=uuid.uuid4().hex,
            type=type,
            title=title,
            content=content,
            tags=[],
            timestamp=1000.0,
            source=source,
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


class TestHandleMemoryExtra:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.facade = FakeFacade()

    def _run(self, args: str, project: str | None = "proj"):
        anyio.run(
            lambda: handle_memory(
                args, project=project, facade=self.facade, send=self.send
            )
        )

    def test_delete_existing_entry(self, setup):
        """Delete an existing entry by full ID."""
        entry = anyio.run(
            lambda: self.facade.memory.add_entry(
                "proj", type="decision", title="T", content="C", source="user"
            )
        )
        self._run(f"delete {entry.id}")
        assert "deleted" in self.texts[0].lower()

    def test_delete_not_found(self, setup):
        """Delete with valid-length prefix that doesn't match anything."""
        self._run("delete abcdef123456")
        assert "not found" in self.texts[0].lower()

    def test_delete_prefix_too_short(self, setup):
        """Delete with prefix shorter than minimum."""
        self._run("delete abc")
        assert "too short" in self.texts[0].lower()

    def test_add_with_current_engine(self, setup):
        """Add entry with current_engine set uses it as source."""
        anyio.run(
            lambda: handle_memory(
                "add decision Title Content here",
                project="proj",
                facade=self.facade,
                current_engine="gemini",
                send=self.send,
            )
        )
        assert "gemini" in self.texts[0]


# ---------------------------------------------------------------------------
# handle_branch — uncovered: merge, discard, link-git, list with branches
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

    async def list(
        self, project: str, status: str | None = None
    ) -> list[_FakeBranch]:
        if status:
            return [b for b in self._branches if b.status == status]
        return list(self._branches)

    async def create(self, project: str, label: str) -> _FakeBranch:
        b = _FakeBranch(branch_id=uuid.uuid4().hex, label=label)
        self._branches.append(b)
        return b

    async def merge(
        self, project: str, branch_id: str
    ) -> _FakeBranch | None:
        for b in self._branches:
            if b.branch_id == branch_id:
                b.status = "merged"
                return b
        return None

    async def discard(
        self, project: str, branch_id: str
    ) -> _FakeBranch | None:
        for b in self._branches:
            if b.branch_id == branch_id:
                b.status = "discarded"
                return b
        return None

    async def link_git_branch(
        self, project: str, branch_id: str, git_branch: str
    ) -> _FakeBranch | None:
        for b in self._branches:
            if b.branch_id == branch_id:
                b.git_branch = git_branch
                return b
        return None


class FakeBranchFacade:
    def __init__(self):
        self.conv_branches = FakeConvBranches()


class TestHandleBranchExtra:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.facade = FakeBranchFacade()

    def _run(self, args: str, project: str | None = "proj"):
        anyio.run(
            lambda: handle_branch(
                args, project=project, facade=self.facade, send=self.send
            )
        )

    def test_list_with_branches(self, setup):
        """List branches shows branch details."""
        anyio.run(lambda: self.facade.conv_branches.create("proj", "feat-a"))
        self._run("list")
        assert "feat-a" in self.texts[0]

    def test_list_with_git_branch(self, setup):
        """Default list shows git branch tag."""
        b = anyio.run(lambda: self.facade.conv_branches.create("proj", "feat-b"))
        anyio.run(
            lambda: self.facade.conv_branches.link_git_branch(
                "proj", b.branch_id, "main"
            )
        )
        self._run("")
        assert "main" in self.texts[0]

    def test_merge_existing(self, setup):
        """Merge an existing branch by full ID."""
        b = anyio.run(lambda: self.facade.conv_branches.create("proj", "to-merge"))
        self._run(f"merge {b.branch_id}")
        assert "merged" in self.texts[0].lower()

    def test_merge_not_found(self, setup):
        """Merge with valid-length prefix that doesn't exist."""
        self._run("merge abcdef123456789")
        assert "not found" in self.texts[0].lower()

    def test_merge_no_args(self, setup):
        """Merge without arguments shows usage."""
        self._run("merge")
        assert "Usage" in self.texts[0]

    def test_discard_existing(self, setup):
        """Discard an existing branch by full ID."""
        b = anyio.run(lambda: self.facade.conv_branches.create("proj", "to-discard"))
        self._run(f"discard {b.branch_id}")
        assert "discarded" in self.texts[0].lower()

    def test_discard_not_found(self, setup):
        """Discard with valid-length prefix that doesn't exist."""
        self._run("discard abcdef123456789")
        assert "not found" in self.texts[0].lower()

    def test_discard_no_args(self, setup):
        """Discard without arguments shows usage."""
        self._run("discard")
        assert "Usage" in self.texts[0]

    def test_link_git_existing(self, setup):
        """Link a git branch to an existing conv branch."""
        b = anyio.run(lambda: self.facade.conv_branches.create("proj", "linkable"))
        self._run(f"link-git {b.branch_id} feature/x")
        assert "linked" in self.texts[0].lower()
        assert "feature/x" in self.texts[0]

    def test_link_git_not_found(self, setup):
        """Link-git with non-existent branch ID."""
        self._run("link-git abcdef123456789 feature/x")
        assert "not found" in self.texts[0].lower()

    def test_link_git_no_args(self, setup):
        """Link-git without enough arguments."""
        self._run("link-git")
        assert "Usage" in self.texts[0]

    def test_link_git_missing_git_branch(self, setup):
        """Link-git with branch id but no git branch name."""
        self._run("link-git abcdef123456")
        assert "Usage" in self.texts[0]


# ---------------------------------------------------------------------------
# handle_review — uncovered: list with entries, approve/reject flows
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

    async def list(
        self, project: str, status: str | None = None
    ) -> list[_FakeReview]:
        if status:
            return [r for r in self._reviews if r.status == status]
        return list(self._reviews)

    async def approve(
        self, project: str, review_id: str, comment: str = ""
    ) -> _FakeReview | None:
        for r in self._reviews:
            if r.review_id == review_id:
                r.status = "approved"
                return r
        return None

    async def reject(
        self, project: str, review_id: str, comment: str = ""
    ) -> _FakeReview | None:
        for r in self._reviews:
            if r.review_id == review_id:
                r.status = "rejected"
                return r
        return None


class FakeReviewFacade:
    def __init__(self):
        self.reviews = FakeReviews()


class TestHandleReviewExtra:
    @pytest.fixture()
    def setup(self):
        self.send, self.texts = _captured_send()
        self.facade = FakeReviewFacade()

    def _run(self, args: str, project: str | None = "proj"):
        anyio.run(
            lambda: handle_review(
                args, project=project, facade=self.facade, send=self.send
            )
        )

    def test_default_with_pending_reviews(self, setup):
        """Default shows pending reviews."""
        rid = uuid.uuid4().hex
        aid = uuid.uuid4().hex
        self.facade.reviews._reviews.append(
            _FakeReview(
                review_id=rid,
                artifact_id=aid,
                artifact_version=1,
                status="pending",
            )
        )
        self._run("")
        assert "Pending reviews" in self.texts[0]

    def test_list_all_statuses(self, setup):
        """List without status filter shows all."""
        rid = uuid.uuid4().hex
        aid = uuid.uuid4().hex
        self.facade.reviews._reviews.append(
            _FakeReview(
                review_id=rid,
                artifact_id=aid,
                artifact_version=1,
                status="approved",
            )
        )
        self._run("list")
        assert "Reviews" in self.texts[0]

    def test_list_empty(self, setup):
        """List with status filter that has no results."""
        self._run("list approved")
        assert "No reviews" in self.texts[0]

    def test_approve_existing(self, setup):
        """Approve an existing review."""
        rid = uuid.uuid4().hex
        aid = uuid.uuid4().hex
        self.facade.reviews._reviews.append(
            _FakeReview(
                review_id=rid,
                artifact_id=aid,
                artifact_version=1,
                status="pending",
            )
        )
        self._run(f"approve {rid}")
        assert "approved" in self.texts[0].lower()

    def test_approve_with_comment(self, setup):
        """Approve with comment."""
        rid = uuid.uuid4().hex
        aid = uuid.uuid4().hex
        self.facade.reviews._reviews.append(
            _FakeReview(
                review_id=rid,
                artifact_id=aid,
                artifact_version=1,
                status="pending",
            )
        )
        self._run(f"approve {rid} LGTM")
        assert "approved" in self.texts[0].lower()

    def test_approve_not_found(self, setup):
        """Approve with non-existent review ID."""
        self._run("approve abcdef123456789")
        assert "not found" in self.texts[0].lower()

    def test_reject_existing(self, setup):
        """Reject an existing review."""
        rid = uuid.uuid4().hex
        aid = uuid.uuid4().hex
        self.facade.reviews._reviews.append(
            _FakeReview(
                review_id=rid,
                artifact_id=aid,
                artifact_version=1,
                status="pending",
            )
        )
        self._run(f"reject {rid} Needs changes")
        assert "rejected" in self.texts[0].lower()

    def test_reject_not_found(self, setup):
        """Reject with non-existent review ID."""
        self._run("reject abcdef123456789")
        assert "not found" in self.texts[0].lower()


# ---------------------------------------------------------------------------
# _resolve_id — uncovered: ambiguous with >5 matches
# ---------------------------------------------------------------------------


class TestResolveIdExtra:
    def test_ambiguous_more_than_5(self):
        """Ambiguous prefix with more than 5 matches shows '... and N more'."""
        items = [f"abcdef{i:06d}" for i in range(8)]
        result = anyio.run(
            lambda: _resolve_id(
                "abcdef",
                fetch_all=AsyncMock(return_value=items),
                get_id=lambda x: x,
                get_label=lambda x: "label",
            )
        )
        assert result[0] is None
        assert "and 3 more" in result[1]
