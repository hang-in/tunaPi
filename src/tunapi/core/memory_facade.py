"""Unified facade for project collaboration memory.

Provides a single entry point for reading and writing project memory,
branch sessions, discussion records, conversation branches, synthesis
artifacts, and review requests.  Designed as the primary API surface
for tunadish and future ``!memory`` / ``!branch`` commands.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..logging import get_logger
from .branch_sessions import BranchRecord, BranchSessionStore
from .conversation_branch import ConversationBranch, ConversationBranchStore
from .discussion_records import DiscussionRecord, DiscussionRecordStore
from .handoff import HandoffURI, build_handoff_uri
from .project_memory import MemoryEntry, ProjectMemoryStore
from .review import ReviewRequest, ReviewStore
from .roundtable.consensus import ExtractedSynthesis, extract_synthesis
from .rt_structured import StructuredRoundtableSession, StructuredRoundtableStore
from .synthesis import SynthesisArtifact, SynthesisStore

if TYPE_CHECKING:
    from .project_memory import EntryType
    from .roundtable import RoundtableSession

logger = get_logger(__name__)

_DEFAULT_BASE = Path.home() / ".tunapi" / "project_memory"


def _extract_from_transcript(
    transcript: list[list[str]],
) -> ExtractedSynthesis | None:
    """Find a synthesizer-style answer in *transcript* and parse it.

    Scans from the end (the synthesizer typically runs last) for the first
    answer that parses into structured synthesis; ``None`` if none qualifies.
    """
    for entry in reversed(transcript):
        answer = entry[1] if len(entry) > 1 else ""
        extracted = extract_synthesis(answer)
        if extracted is not None:
            return extracted
    return None


# -- DTO for structured context reads (used by tunadish) -------------------


@dataclass(slots=True)
class ProjectContextDTO:
    """Structured snapshot of a project's collaboration state.

    ``markdown`` contains the same text as :meth:`get_project_context`
    for backward compatibility; the other fields expose the raw data
    so that tunadish can render its own UI without parsing Markdown.
    """

    project_alias: str | None = None
    project_path: str | None = None
    default_engine: str | None = None
    memory_entries: list[MemoryEntry] = field(default_factory=list)
    active_branches: list[BranchRecord] = field(default_factory=list)
    conv_branches: list[ConversationBranch] = field(default_factory=list)
    discussions: list[DiscussionRecord] = field(default_factory=list)
    pending_reviews: list[ReviewRequest] = field(default_factory=list)
    synthesis_artifacts: list[SynthesisArtifact] = field(default_factory=list)
    structured_sessions: list[StructuredRoundtableSession] = field(default_factory=list)
    markdown: str = ""


class ProjectMemoryFacade:
    """Unified read/write API for project-level collaboration state.

    Instantiate once (e.g. in backend startup) and pass to command
    handlers or expose to tunadish.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base = base_dir or _DEFAULT_BASE
        self.memory = ProjectMemoryStore(self._base)
        self.branches = BranchSessionStore(self._base)
        self.discussions = DiscussionRecordStore(self._base)
        self.conv_branches = ConversationBranchStore(self._base)
        self.synthesis = SynthesisStore(self._base)
        self.reviews = ReviewStore(self._base)
        self.rt_structured = StructuredRoundtableStore(self._base)

    # ------------------------------------------------------------------
    # Memory convenience
    # ------------------------------------------------------------------

    async def add_memory(
        self,
        project: str,
        *,
        type: EntryType,
        title: str,
        content: str,
        source: str,
        tags: list[str] | None = None,
    ) -> MemoryEntry:
        return await self.memory.add_entry(
            project,
            type=type,
            title=title,
            content=content,
            source=source,
            tags=tags,
        )

    # ------------------------------------------------------------------
    # Discussion convenience
    # ------------------------------------------------------------------

    async def save_roundtable(
        self,
        session: RoundtableSession,
        project: str,
        *,
        summary: str | None = None,
        branch_name: str | None = None,
        auto_synthesis: bool = False,
        auto_structured: bool = False,
    ) -> DiscussionRecord:
        """Persist a completed roundtable as a discussion record.

        If *branch_name* is provided, the discussion is linked to that
        branch bidirectionally.  If *auto_synthesis* is ``True``, a
        :class:`SynthesisArtifact` is also created.  If
        *auto_structured* is ``True``, a
        :class:`StructuredRoundtableSession` is also stored.
        Both are best-effort — failures are logged (not fatal), never silent.
        """
        record = await self.discussions.create_record(
            project,
            discussion_id=session.thread_id,
            topic=session.topic,
            participants=session.engines,
            rounds=session.current_round,
            transcript=session.transcript,
            summary=summary,
            branch_name=branch_name,
        )
        # These are best-effort enrichments — a failure must not lose the
        # discussion record, but it should be visible (not silently swallowed).
        if branch_name:
            try:
                await self.link_discussion_to_branch(
                    project, record.discussion_id, branch_name
                )
            except Exception:  # noqa: BLE001 — best-effort; logged, not fatal
                logger.warning("roundtable.link_branch_failed", exc_info=True)
        if auto_synthesis:
            try:
                await self.save_synthesis_from_discussion(project, record.discussion_id)
            except Exception:  # noqa: BLE001
                logger.warning("roundtable.save_synthesis_failed", exc_info=True)
        if auto_structured:
            try:
                structured = StructuredRoundtableStore.from_roundtable_session(
                    session, project
                )
                await self.rt_structured.create(
                    project,
                    session_id=structured.session_id,
                    topic=structured.topic,
                    stages=structured.stages,
                    participants=structured.participants,
                    utterances=structured.utterances,
                )
                await self.rt_structured.complete(project, structured.session_id)
            except Exception:  # noqa: BLE001
                logger.warning("roundtable.save_structured_failed", exc_info=True)
        return record

    async def link_discussion_to_branch(
        self,
        project: str,
        discussion_id: str,
        branch_name: str,
    ) -> bool:
        """Bidirectional link: set branch_name on discussion, add discussion_id to branch."""
        store = self.discussions._store_for(project)
        async with store._lock:
            store._reload_locked_if_needed()
            record = store._state.records.get(discussion_id)
            if record is None:
                return False
            record.branch_name = branch_name
            store._save_locked()

        b_store = self.branches._store_for(project)
        async with b_store._lock:
            b_store._reload_locked_if_needed()
            branch = b_store._state.branches.get(branch_name)
            if branch is not None and discussion_id not in branch.discussion_ids:
                branch.discussion_ids.append(discussion_id)
                branch.updated_at = time.time()
                b_store._save_locked()

        return True

    # ------------------------------------------------------------------
    # Synthesis convenience
    # ------------------------------------------------------------------

    async def save_synthesis_from_discussion(
        self,
        project: str,
        discussion_id: str,
    ) -> SynthesisArtifact | None:
        """Create a synthesis artifact from an existing discussion record.

        Maps ``summary`` → ``thesis`` and copies ``action_items``.
        Returns ``None`` if the discussion is not found.
        """
        record = await self.discussions.get_record(project, discussion_id)
        if record is None:
            return None
        proto = SynthesisStore.from_discussion_record(record)
        # Populate agreements/disagreements/open_questions from the
        # synthesizer's structured output when present (else summary->thesis).
        extracted = _extract_from_transcript(record.transcript)
        thesis = extracted.thesis if (extracted and extracted.thesis) else proto.thesis
        return await self.synthesis.create(
            project,
            source_type=proto.source_type,
            source_id=proto.source_id,
            thesis=thesis,
            agreements=list(extracted.agreements) if extracted else None,
            disagreements=list(extracted.disagreements) if extracted else None,
            open_questions=list(extracted.open_questions) if extracted else None,
            action_items=list(proto.action_items),
        )

    # ------------------------------------------------------------------
    # Review convenience
    # ------------------------------------------------------------------

    async def request_review_for_synthesis(
        self,
        project: str,
        artifact_id: str,
    ) -> ReviewRequest | None:
        """Create a review request for a synthesis artifact.

        Returns ``None`` if the artifact is not found.
        """
        artifact = await self.synthesis.get(project, artifact_id)
        if artifact is None:
            return None
        return await self.reviews.request_review(
            project,
            artifact_id=artifact_id,
            artifact_version=artifact.version,
        )

    # ------------------------------------------------------------------
    # Aggregated project context (read-heavy, for tunadish)
    # ------------------------------------------------------------------

    async def get_project_context_dto(
        self,
        project: str,
        *,
        project_path: str | None = None,
        default_engine: str | None = None,
    ) -> ProjectContextDTO:
        """Return structured project state for tunadish UI rendering.

        *project_path* and *default_engine* are optional metadata from
        the transport runtime; the facade itself does not resolve them.
        """
        memory_entries = await self.memory.list_entries(project, limit=50)
        active_branches = await self.branches.list_branches(project, status="active")
        conv_branches = await self.conv_branches.list(project, status="active")
        open_disc = await self.discussions.list_records(project, status="open")
        resolved_disc = await self.discussions.list_records(project, status="resolved")
        discussions = open_disc + resolved_disc
        pending_reviews = await self.reviews.list(project, status="pending")
        synthesis_artifacts = await self.synthesis.list(project)
        structured_sessions = await self.rt_structured.list(project)

        markdown = self._render_markdown(
            memory_entries=memory_entries,
            active_branches=active_branches,
            conv_branches=conv_branches,
            discussions=discussions,
            pending_reviews=pending_reviews,
        )

        return ProjectContextDTO(
            project_alias=project,
            project_path=project_path,
            default_engine=default_engine,
            memory_entries=memory_entries,
            active_branches=active_branches,
            conv_branches=conv_branches,
            discussions=discussions,
            pending_reviews=pending_reviews,
            synthesis_artifacts=synthesis_artifacts,
            structured_sessions=structured_sessions,
            markdown=markdown,
        )

    async def get_project_context(self, project: str) -> str:
        """Full context summary for agent prompts (Markdown string)."""
        dto = await self.get_project_context_dto(project)
        return dto.markdown

    @staticmethod
    def _render_markdown(
        *,
        memory_entries: list[MemoryEntry],
        active_branches: list[BranchRecord],
        conv_branches: list[ConversationBranch],
        discussions: list[DiscussionRecord],
        pending_reviews: list[ReviewRequest],
    ) -> str:
        parts: list[str] = []

        # Memory entries grouped by type
        if memory_entries:
            by_type: dict[str, list[MemoryEntry]] = {}
            for e in memory_entries:
                by_type.setdefault(e.type, []).append(e)
            for entry_type in ("context", "decision", "review", "idea"):
                typed = by_type.get(entry_type, [])[:5]
                if not typed:
                    continue
                label = entry_type.capitalize()
                lines = [f"## {label}"]
                for e in typed:
                    tag_str = f" [{', '.join(e.tags)}]" if e.tags else ""
                    lines.append(f"- **{e.title}**{tag_str}: {e.content[:200]}")
                parts.append("\n".join(lines))

        # Git-level branches
        if active_branches:
            branch_lines = [
                f"- **{b.branch_name}**: {b.description}"
                if b.description
                else f"- **{b.branch_name}**"
                for b in active_branches
            ]
            parts.append("## Active Branches\n" + "\n".join(branch_lines))

        # Conversation branches
        if conv_branches:
            conv_lines = []
            for cb in conv_branches:
                line = f"- **{cb.label}**"
                if cb.git_branch:
                    line += f" → `{cb.git_branch}`"
                conv_lines.append(line)
            parts.append("## Conversation Branches\n" + "\n".join(conv_lines))

        # Discussions
        if discussions:
            disc_lines = []
            for d in discussions:
                status_tag = f"[{d.status}]"
                participant_str = ", ".join(d.participants)
                line = f"- {status_tag} **{d.topic}** ({participant_str})"
                if d.branch_name:
                    line += f" on `{d.branch_name}`"
                if d.resolution:
                    line += f" — {d.resolution[:100]}"
                disc_lines.append(line)
            parts.append("## Discussions\n" + "\n".join(disc_lines))

        # Pending reviews
        if pending_reviews:
            review_lines = [
                f"- artifact `{r.artifact_id}` v{r.artifact_version}"
                for r in pending_reviews
            ]
            parts.append("## Pending Reviews\n" + "\n".join(review_lines))

        return "\n\n".join(parts) if parts else ""

    # ------------------------------------------------------------------
    # Handoff URI
    # ------------------------------------------------------------------

    async def get_handoff_uri(
        self,
        project: str,
        *,
        session_id: str | None = None,
        branch_id: str | None = None,
        focus: str | None = None,
        pending_run_id: str | None = None,
    ) -> str:
        """Build a handoff URI for the given project context.

        If no *focus* is given but there are pending reviews, the
        first pending review is used as focus.  If no *branch_id* is
        given but there are active conversation branches, the most
        recent one is used.
        """
        # Auto-resolve branch
        if branch_id is None:
            active = await self.conv_branches.list(project, status="active")
            if active:
                branch_id = active[0].branch_id

        # Auto-resolve focus from pending reviews
        if focus is None:
            pending = await self.reviews.list(project, status="pending")
            if pending:
                focus = pending[0].review_id

        return build_handoff_uri(
            HandoffURI(
                project=project,
                session_id=session_id,
                branch_id=branch_id,
                focus=focus,
                pending_run_id=pending_run_id,
            )
        )
