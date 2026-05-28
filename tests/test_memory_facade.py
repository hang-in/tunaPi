"""Integration tests for core/memory_facade.py."""

from __future__ import annotations

from dataclasses import dataclass, field

import anyio
import pytest

from tunapi.core.memory_facade import ProjectMemoryFacade

pytestmark = pytest.mark.anyio


@dataclass
class _FakeSession:
    thread_id: str = "t1"
    topic: str = "API design"
    engines: list[str] = field(default_factory=lambda: ["claude", "gemini"])
    total_rounds: int = 1
    current_round: int = 1
    transcript: list[tuple[str, str]] = field(
        default_factory=lambda: [("claude", "Use REST"), ("gemini", "Agree")]
    )
    cancel_event: object = field(default_factory=lambda: anyio.Event())
    completed: bool = True
    channel_id: str = "ch1"


def _fake_session(**kwargs) -> _FakeSession:
    return _FakeSession(**kwargs)


class TestEmptyProject:
    async def test_empty_context(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        assert await facade.get_project_context("empty") == ""

    async def test_empty_branch_list(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        assert await facade.branches.list_branches("empty") == []

    async def test_empty_discussion_list(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        assert await facade.discussions.list_records("empty") == []


class TestContextAggregation:
    async def test_memory_only(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        await facade.add_memory(
            "proj",
            type="decision",
            title="Use REST",
            content="Simple and well-supported",
            source="claude",
        )
        ctx = await facade.get_project_context("proj")
        assert "## Decision" in ctx
        assert "Use REST" in ctx
        assert "## Active Branches" not in ctx
        assert "## Discussions" not in ctx

    async def test_branches_only(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        await facade.branches.create_branch(
            "proj", "feat/auth", description="OAuth2 login"
        )
        ctx = await facade.get_project_context("proj")
        assert "## Active Branches" in ctx
        assert "feat/auth" in ctx
        assert "OAuth2 login" in ctx

    async def test_discussions_only(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        await facade.discussions.create_record(
            "proj",
            topic="API review",
            participants=["claude", "gemini"],
            rounds=1,
            transcript=[("claude", "ok")],
        )
        ctx = await facade.get_project_context("proj")
        assert "## Discussions" in ctx
        assert "[open]" in ctx
        assert "API review" in ctx
        assert "claude, gemini" in ctx

    async def test_full_aggregation(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)

        await facade.add_memory(
            "proj",
            type="decision",
            title="DB choice",
            content="PostgreSQL",
            source="claude",
        )
        await facade.branches.create_branch(
            "proj", "feat/api", description="REST endpoints"
        )
        r = await facade.discussions.create_record(
            "proj",
            topic="Schema design",
            participants=["claude"],
            rounds=1,
            transcript=[("claude", "use jsonb")],
        )
        await facade.discussions.update_resolution(
            "proj", r.discussion_id, "Use JSONB columns"
        )

        ctx = await facade.get_project_context("proj")
        assert "## Decision" in ctx
        assert "DB choice" in ctx
        assert "## Active Branches" in ctx
        assert "feat/api" in ctx
        assert "## Discussions" in ctx
        assert "[resolved]" in ctx
        assert "Use JSONB columns" in ctx

    async def test_resolved_discussion_shows_resolution(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        r = await facade.discussions.create_record(
            "proj",
            topic="T",
            participants=["claude"],
            rounds=1,
            transcript=[("claude", "a")],
        )
        await facade.discussions.update_resolution("proj", r.discussion_id, "Done it")
        ctx = await facade.get_project_context("proj")
        assert "Done it" in ctx

    async def test_discussion_with_branch_name_in_context(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        await facade.branches.create_branch("proj", "feat/x")
        await facade.discussions.create_record(
            "proj",
            topic="X design",
            participants=["claude"],
            rounds=1,
            transcript=[("claude", "a")],
            branch_name="feat/x",
        )
        ctx = await facade.get_project_context("proj")
        assert "on `feat/x`" in ctx


class TestSaveRoundtable:
    async def test_save_without_branch(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        session = _fake_session()
        record = await facade.save_roundtable(session, "proj")
        assert record.discussion_id == "t1"
        assert record.project_alias == "proj"
        assert record.branch_name is None

        fetched = await facade.discussions.get_record("proj", "t1")
        assert fetched is not None
        assert fetched.topic == "API design"

    async def test_save_with_branch(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        await facade.branches.create_branch("proj", "feat/api")
        session = _fake_session()
        record = await facade.save_roundtable(session, "proj", branch_name="feat/api")
        assert record.branch_name == "feat/api"

        # Branch should have discussion_id linked
        branch = await facade.branches.get_branch("proj", "feat/api")
        assert branch is not None
        assert "t1" in branch.discussion_ids

    async def test_save_with_summary(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        session = _fake_session()
        record = await facade.save_roundtable(session, "proj", summary="Agreed on REST")
        assert record.summary == "Agreed on REST"

    async def test_auto_synthesis_creates_artifact(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        session = _fake_session()
        record = await facade.save_roundtable(
            session, "proj", summary="Use REST", auto_synthesis=True
        )
        # Discussion created
        assert record.discussion_id == "t1"

        # Synthesis also created
        artifacts = await facade.synthesis.list("proj")
        assert len(artifacts) == 1
        assert artifacts[0].source_id == "t1"
        assert artifacts[0].thesis == "Use REST"
        assert artifacts[0].source_type == "discussion"

    async def test_auto_synthesis_false_by_default(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        session = _fake_session()
        await facade.save_roundtable(session, "proj", summary="X")

        artifacts = await facade.synthesis.list("proj")
        assert len(artifacts) == 0

    async def test_auto_synthesis_failure_suppressed(self, tmp_path):
        """Synthesis failure must not prevent discussion record creation."""
        facade = ProjectMemoryFacade(tmp_path)

        # Sabotage synthesis store to raise on create
        async def broken_create(*a, **kw):
            raise RuntimeError("disk full")

        facade.synthesis.create = broken_create

        session = _fake_session()
        record = await facade.save_roundtable(
            session, "proj", summary="X", auto_synthesis=True
        )
        # Discussion still created despite synthesis failure
        assert record.discussion_id == "t1"
        fetched = await facade.discussions.get_record("proj", "t1")
        assert fetched is not None

    async def test_auto_structured_creates_session(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        session = _fake_session()
        await facade.save_roundtable(session, "proj", auto_structured=True)
        structured = await facade.rt_structured.list("proj")
        assert len(structured) == 1
        assert structured[0].session_id == "t1"
        assert structured[0].status == "completed"
        assert len(structured[0].participants) == 2
        assert len(structured[0].utterances) == 2

    async def test_auto_structured_false_by_default(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        session = _fake_session()
        await facade.save_roundtable(session, "proj")
        structured = await facade.rt_structured.list("proj")
        assert len(structured) == 0

    async def test_auto_structured_failure_suppressed(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)

        async def broken(*a, **kw):
            raise RuntimeError("boom")

        facade.rt_structured.create = broken
        session = _fake_session()
        record = await facade.save_roundtable(session, "proj", auto_structured=True)
        assert record.discussion_id == "t1"


class TestLinkDiscussionToBranch:
    async def test_bidirectional_link(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        await facade.branches.create_branch("proj", "feat/x")
        r = await facade.discussions.create_record(
            "proj",
            topic="T",
            participants=["claude"],
            rounds=1,
            transcript=[("claude", "a")],
        )

        result = await facade.link_discussion_to_branch(
            "proj", r.discussion_id, "feat/x"
        )
        assert result is True

        # Discussion side
        disc = await facade.discussions.get_record("proj", r.discussion_id)
        assert disc is not None
        assert disc.branch_name == "feat/x"

        # Branch side
        branch = await facade.branches.get_branch("proj", "feat/x")
        assert branch is not None
        assert r.discussion_id in branch.discussion_ids

    async def test_link_nonexistent_discussion(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        await facade.branches.create_branch("proj", "feat/x")
        result = await facade.link_discussion_to_branch("proj", "nope", "feat/x")
        assert result is False

    async def test_link_nonexistent_branch_still_updates_discussion(self, tmp_path):
        """Discussion side is updated even if branch doesn't exist."""
        facade = ProjectMemoryFacade(tmp_path)
        r = await facade.discussions.create_record(
            "proj",
            topic="T",
            participants=["claude"],
            rounds=1,
            transcript=[("claude", "a")],
        )
        result = await facade.link_discussion_to_branch(
            "proj", r.discussion_id, "nonexistent"
        )
        assert result is True
        disc = await facade.discussions.get_record("proj", r.discussion_id)
        assert disc is not None
        assert disc.branch_name == "nonexistent"

    async def test_link_idempotent(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        await facade.branches.create_branch("proj", "feat/x")
        r = await facade.discussions.create_record(
            "proj",
            topic="T",
            participants=["claude"],
            rounds=1,
            transcript=[("claude", "a")],
        )
        await facade.link_discussion_to_branch("proj", r.discussion_id, "feat/x")
        await facade.link_discussion_to_branch("proj", r.discussion_id, "feat/x")
        branch = await facade.branches.get_branch("proj", "feat/x")
        assert branch is not None
        assert branch.discussion_ids.count(r.discussion_id) == 1


# -- P0 integration: ConversationBranch, Synthesis, Review ----------------


class TestConvBranchInContext:
    async def test_active_conv_branch_in_context(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        await facade.conv_branches.create("proj", "experiment: new API")
        ctx = await facade.get_project_context("proj")
        assert "## Conversation Branches" in ctx
        assert "experiment: new API" in ctx

    async def test_conv_branch_with_git_link_in_context(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        await facade.conv_branches.create(
            "proj", "auth refactor", git_branch="feat/auth"
        )
        ctx = await facade.get_project_context("proj")
        assert "auth refactor" in ctx
        assert "`feat/auth`" in ctx

    async def test_merged_conv_branch_not_in_context(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        cb = await facade.conv_branches.create("proj", "done branch")
        await facade.conv_branches.merge("proj", cb.branch_id)
        ctx = await facade.get_project_context("proj")
        assert "done branch" not in ctx


class TestPendingReviewInContext:
    async def test_pending_review_in_context(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        art = await facade.synthesis.create(
            "proj", source_type="manual", source_id="m1", thesis="thesis"
        )
        await facade.reviews.request_review(
            "proj", artifact_id=art.artifact_id, artifact_version=1
        )
        ctx = await facade.get_project_context("proj")
        assert "## Pending Reviews" in ctx
        assert art.artifact_id in ctx

    async def test_approved_review_not_in_context(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        art = await facade.synthesis.create(
            "proj", source_type="manual", source_id="m1", thesis="thesis"
        )
        rev = await facade.reviews.request_review("proj", artifact_id=art.artifact_id)
        await facade.reviews.approve("proj", rev.review_id)
        ctx = await facade.get_project_context("proj")
        assert "## Pending Reviews" not in ctx


class TestSaveSynthesisFromDiscussion:
    async def test_creates_artifact(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        disc = await facade.discussions.create_record(
            "proj",
            topic="API design",
            participants=["claude"],
            rounds=1,
            transcript=[("claude", "use REST")],
            summary="Agreed on REST",
        )
        await facade.discussions.add_action_item(
            "proj", disc.discussion_id, "Write spec"
        )

        art = await facade.save_synthesis_from_discussion("proj", disc.discussion_id)
        assert art is not None
        assert art.thesis == "Agreed on REST"
        assert art.source_type == "discussion"
        assert art.source_id == disc.discussion_id
        assert len(art.action_items) == 1

        # Persisted
        fetched = await facade.synthesis.get("proj", art.artifact_id)
        assert fetched is not None

    async def test_nonexistent_discussion_returns_none(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        assert await facade.save_synthesis_from_discussion("proj", "nope") is None


class TestRequestReviewForSynthesis:
    async def test_creates_review(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        art = await facade.synthesis.create(
            "proj", source_type="manual", source_id="m1", thesis="thesis", version=2
        )
        rev = await facade.request_review_for_synthesis("proj", art.artifact_id)
        assert rev is not None
        assert rev.artifact_id == art.artifact_id
        assert rev.artifact_version == 2
        assert rev.status == "pending"

    async def test_nonexistent_artifact_returns_none(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        assert await facade.request_review_for_synthesis("proj", "nope") is None


class TestFullP0Flow:
    async def test_discussion_to_synthesis_to_review(self, tmp_path):
        """End-to-end: discussion → synthesis → review → context."""
        facade = ProjectMemoryFacade(tmp_path)

        # 1. Create discussion
        disc = await facade.discussions.create_record(
            "proj",
            topic="DB choice",
            participants=["claude", "gemini"],
            rounds=1,
            transcript=[("claude", "PostgreSQL"), ("gemini", "Agree")],
            summary="Use PostgreSQL",
        )

        # 2. Create synthesis from discussion
        art = await facade.save_synthesis_from_discussion("proj", disc.discussion_id)
        assert art is not None
        assert art.thesis == "Use PostgreSQL"

        # 3. Request review
        rev = await facade.request_review_for_synthesis("proj", art.artifact_id)
        assert rev is not None
        assert rev.status == "pending"

        # 4. Context shows pending review
        ctx = await facade.get_project_context("proj")
        assert "## Pending Reviews" in ctx
        assert "## Discussions" in ctx

        # 5. Approve review
        await facade.reviews.approve("proj", rev.review_id, comment="LGTM")

        # 6. Pending review disappears from context
        ctx2 = await facade.get_project_context("proj")
        assert "## Pending Reviews" not in ctx2
        assert "## Discussions" in ctx2


# -- DTO tests ---------------------------------------------------------------


class TestProjectContextDTO:
    async def test_empty_project(self, tmp_path):
        from tunapi.core.memory_facade import ProjectContextDTO

        facade = ProjectMemoryFacade(tmp_path)
        dto = await facade.get_project_context_dto("empty")
        assert isinstance(dto, ProjectContextDTO)
        assert dto.project_alias == "empty"
        assert dto.project_path is None
        assert dto.default_engine is None
        assert dto.memory_entries == []
        assert dto.active_branches == []
        assert dto.conv_branches == []
        assert dto.discussions == []
        assert dto.pending_reviews == []
        assert dto.synthesis_artifacts == []
        assert dto.structured_sessions == []
        assert dto.markdown == ""

    async def test_dto_project_metadata(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        dto = await facade.get_project_context_dto(
            "proj",
            project_path="/home/user/proj",
            default_engine="claude",
        )
        assert dto.project_alias == "proj"
        assert dto.project_path == "/home/user/proj"
        assert dto.default_engine == "claude"

    async def test_dto_fields_populated(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)

        # Populate all stores
        await facade.add_memory(
            "proj",
            type="decision",
            title="DB choice",
            content="PostgreSQL",
            source="claude",
        )
        await facade.branches.create_branch(
            "proj", "feat/api", description="REST endpoints"
        )
        await facade.conv_branches.create("proj", "experiment: caching")
        await facade.discussions.create_record(
            "proj",
            topic="API review",
            participants=["claude"],
            rounds=1,
            transcript=[("claude", "ok")],
        )
        art = await facade.synthesis.create(
            "proj",
            source_type="manual",
            source_id="m1",
            thesis="Use REST",
        )
        await facade.reviews.request_review("proj", artifact_id=art.artifact_id)

        dto = await facade.get_project_context_dto("proj")

        assert len(dto.memory_entries) == 1
        assert dto.memory_entries[0].title == "DB choice"

        assert len(dto.active_branches) == 1
        assert dto.active_branches[0].branch_name == "feat/api"

        assert len(dto.conv_branches) == 1
        assert dto.conv_branches[0].label == "experiment: caching"

        assert len(dto.discussions) == 1
        assert dto.discussions[0].topic == "API review"

        assert len(dto.pending_reviews) == 1
        assert dto.pending_reviews[0].artifact_id == art.artifact_id

        assert len(dto.synthesis_artifacts) == 1
        assert dto.synthesis_artifacts[0].thesis == "Use REST"

    async def test_dto_markdown_matches_get_project_context(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        await facade.add_memory(
            "proj",
            type="decision",
            title="Use Rust",
            content="Performance",
            source="user",
        )
        await facade.branches.create_branch("proj", "feat/x")

        dto = await facade.get_project_context_dto("proj")
        plain = await facade.get_project_context("proj")

        assert dto.markdown == plain
        assert "Use Rust" in dto.markdown

    async def test_dto_only_active_conv_branches(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        cb = await facade.conv_branches.create("proj", "done")
        await facade.conv_branches.merge("proj", cb.branch_id)
        await facade.conv_branches.create("proj", "active")

        dto = await facade.get_project_context_dto("proj")
        assert len(dto.conv_branches) == 1
        assert dto.conv_branches[0].label == "active"

    async def test_dto_synthesis_includes_all(self, tmp_path):
        """synthesis_artifacts returns all (not filtered by source_type)."""
        facade = ProjectMemoryFacade(tmp_path)
        await facade.synthesis.create(
            "proj", source_type="roundtable", source_id="r1", thesis="A"
        )
        await facade.synthesis.create(
            "proj", source_type="manual", source_id="m1", thesis="B"
        )

        dto = await facade.get_project_context_dto("proj")
        assert len(dto.synthesis_artifacts) == 2

    async def test_dto_structured_sessions_populated(self, tmp_path):
        facade = ProjectMemoryFacade(tmp_path)
        session = _fake_session()
        await facade.save_roundtable(session, "proj", auto_structured=True)
        dto = await facade.get_project_context_dto("proj")
        assert len(dto.structured_sessions) == 1
        assert dto.structured_sessions[0].session_id == "t1"
        assert dto.structured_sessions[0].status == "completed"


# -- Handoff URI tests -------------------------------------------------------


class TestGetHandoffURI:
    async def test_project_only(self, tmp_path):
        from tunapi.core.handoff import parse_handoff_uri

        facade = ProjectMemoryFacade(tmp_path)
        uri = await facade.get_handoff_uri("proj")
        parsed = parse_handoff_uri(uri)
        assert parsed is not None
        assert parsed.project == "proj"

    async def test_explicit_params(self, tmp_path):
        from tunapi.core.handoff import parse_handoff_uri

        facade = ProjectMemoryFacade(tmp_path)
        uri = await facade.get_handoff_uri(
            "proj",
            session_id="sess1",
            branch_id="br1",
            focus="disc1",
            pending_run_id="run1",
        )
        parsed = parse_handoff_uri(uri)
        assert parsed is not None
        assert parsed.session_id == "sess1"
        assert parsed.branch_id == "br1"
        assert parsed.focus == "disc1"
        assert parsed.pending_run_id == "run1"

    async def test_auto_resolve_branch(self, tmp_path):
        from tunapi.core.handoff import parse_handoff_uri

        facade = ProjectMemoryFacade(tmp_path)
        cb = await facade.conv_branches.create("proj", "active branch")
        uri = await facade.get_handoff_uri("proj")
        parsed = parse_handoff_uri(uri)
        assert parsed is not None
        assert parsed.branch_id == cb.branch_id

    async def test_auto_resolve_focus_from_review(self, tmp_path):
        from tunapi.core.handoff import parse_handoff_uri

        facade = ProjectMemoryFacade(tmp_path)
        art = await facade.synthesis.create(
            "proj", source_type="manual", source_id="m1", thesis="t"
        )
        rev = await facade.reviews.request_review("proj", artifact_id=art.artifact_id)
        uri = await facade.get_handoff_uri("proj")
        parsed = parse_handoff_uri(uri)
        assert parsed is not None
        assert parsed.focus == rev.review_id

    async def test_explicit_overrides_auto(self, tmp_path):
        from tunapi.core.handoff import parse_handoff_uri

        facade = ProjectMemoryFacade(tmp_path)
        await facade.conv_branches.create("proj", "auto branch")
        uri = await facade.get_handoff_uri("proj", branch_id="explicit_br")
        parsed = parse_handoff_uri(uri)
        assert parsed is not None
        assert parsed.branch_id == "explicit_br"
