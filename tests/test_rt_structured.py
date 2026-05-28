"""Tests for core/rt_structured.py."""

from __future__ import annotations

from dataclasses import dataclass, field

import anyio
import pytest

from tunapi.core.rt_participant import build_participants_from_engines
from tunapi.core.rt_structured import (
    StructuredRoundtableSession,
    StructuredRoundtableStore,
)
from tunapi.core.rt_utterance import Utterance

pytestmark = pytest.mark.anyio


def _make_participants():
    return build_participants_from_engines(["claude", "gemini"])


def _make_utterance(stage="round_1", engine="claude", text="hello"):
    from tunapi.core.project_memory import generate_entry_id

    p = build_participants_from_engines([engine])
    return Utterance(
        utterance_id=generate_entry_id(),
        stage=stage,
        participant_id=p[0].participant_id,
        engine=engine,
        role=engine,
        output_text=text,
    )


class TestStructuredRoundtableStore:
    async def test_create_and_get(self, tmp_path):
        store = StructuredRoundtableStore(tmp_path)
        participants = _make_participants()
        session = await store.create(
            "proj",
            topic="API design",
            stages=["round_1"],
            participants=participants,
        )
        assert isinstance(session, StructuredRoundtableSession)
        assert session.topic == "API design"
        assert session.status == "active"
        assert session.current_stage == "round_1"
        assert len(session.participants) == 2
        assert session.created_at != ""

        fetched = await store.get("proj", session.session_id)
        assert fetched is not None
        assert fetched.topic == "API design"

    async def test_get_nonexistent(self, tmp_path):
        store = StructuredRoundtableStore(tmp_path)
        assert await store.get("proj", "nope") is None

    async def test_explicit_session_id(self, tmp_path):
        store = StructuredRoundtableStore(tmp_path)
        session = await store.create(
            "proj",
            session_id="custom_id",
            topic="T",
            stages=["round_1"],
            participants=_make_participants(),
        )
        assert session.session_id == "custom_id"

    async def test_list_all(self, tmp_path):
        store = StructuredRoundtableStore(tmp_path)
        await store.create(
            "proj", topic="A", stages=["round_1"], participants=_make_participants()
        )
        await store.create(
            "proj", topic="B", stages=["round_1"], participants=_make_participants()
        )
        sessions = await store.list("proj")
        assert len(sessions) == 2

    async def test_list_filter_status(self, tmp_path):
        store = StructuredRoundtableStore(tmp_path)
        s1 = await store.create(
            "proj", topic="A", stages=["round_1"], participants=_make_participants()
        )
        await store.create(
            "proj", topic="B", stages=["round_1"], participants=_make_participants()
        )
        await store.complete("proj", s1.session_id)

        active = await store.list("proj", status="active")
        assert len(active) == 1
        assert active[0].topic == "B"

        completed = await store.list("proj", status="completed")
        assert len(completed) == 1
        assert completed[0].session_id == s1.session_id

    async def test_add_utterance(self, tmp_path):
        store = StructuredRoundtableStore(tmp_path)
        session = await store.create(
            "proj", topic="T", stages=["round_1"], participants=_make_participants()
        )
        utt = _make_utterance()
        assert await store.add_utterance("proj", session.session_id, utt) is True

        fetched = await store.get("proj", session.session_id)
        assert fetched is not None
        assert len(fetched.utterances) == 1
        assert fetched.utterances[0].output_text == "hello"

    async def test_add_utterance_updates_stage(self, tmp_path):
        store = StructuredRoundtableStore(tmp_path)
        session = await store.create(
            "proj",
            topic="T",
            stages=["round_1", "round_2"],
            participants=_make_participants(),
        )
        assert session.current_stage == "round_1"

        utt = _make_utterance(stage="round_2")
        await store.add_utterance("proj", session.session_id, utt)

        fetched = await store.get("proj", session.session_id)
        assert fetched is not None
        assert fetched.current_stage == "round_2"

    async def test_add_utterance_nonexistent(self, tmp_path):
        store = StructuredRoundtableStore(tmp_path)
        utt = _make_utterance()
        assert await store.add_utterance("proj", "nope", utt) is False

    async def test_complete(self, tmp_path):
        store = StructuredRoundtableStore(tmp_path)
        s = await store.create(
            "proj", topic="T", stages=["round_1"], participants=_make_participants()
        )
        result = await store.complete("proj", s.session_id)
        assert result is not None
        assert result.status == "completed"
        assert result.completed_at is not None

    async def test_cancel(self, tmp_path):
        store = StructuredRoundtableStore(tmp_path)
        s = await store.create(
            "proj", topic="T", stages=["round_1"], participants=_make_participants()
        )
        result = await store.cancel("proj", s.session_id)
        assert result is not None
        assert result.status == "cancelled"
        assert result.completed_at is not None

    async def test_transition_nonexistent(self, tmp_path):
        store = StructuredRoundtableStore(tmp_path)
        assert await store.complete("proj", "nope") is None
        assert await store.cancel("proj", "nope") is None

    async def test_persistence(self, tmp_path):
        store1 = StructuredRoundtableStore(tmp_path)
        s = await store1.create(
            "proj",
            topic="Persisted",
            stages=["round_1"],
            participants=_make_participants(),
        )
        utt = _make_utterance()
        await store1.add_utterance("proj", s.session_id, utt)

        store2 = StructuredRoundtableStore(tmp_path)
        fetched = await store2.get("proj", s.session_id)
        assert fetched is not None
        assert fetched.topic == "Persisted"
        assert len(fetched.utterances) == 1

    async def test_projects_isolated(self, tmp_path):
        store = StructuredRoundtableStore(tmp_path)
        await store.create(
            "a", topic="A", stages=["round_1"], participants=_make_participants()
        )
        await store.create(
            "b", topic="B", stages=["round_1"], participants=_make_participants()
        )
        assert len(await store.list("a")) == 1
        assert len(await store.list("b")) == 1


class TestFromRoundtableSession:
    def test_basic_conversion(self):
        @dataclass
        class FakeSession:
            thread_id: str = "t1"
            channel_id: str = "ch1"
            topic: str = "API design"
            engines: list[str] = field(default_factory=lambda: ["claude", "gemini"])
            total_rounds: int = 2
            current_round: int = 2
            transcript: list[tuple[str, str]] = field(
                default_factory=lambda: [
                    ("claude", "r1 claude"),
                    ("gemini", "r1 gemini"),
                    ("claude", "r2 claude"),
                    ("gemini", "r2 gemini"),
                ]
            )
            cancel_event: object = field(default_factory=lambda: anyio.Event())
            completed: bool = True

        session = FakeSession()
        structured = StructuredRoundtableStore.from_roundtable_session(session, "proj")

        assert structured.session_id == "t1"
        assert structured.project_alias == "proj"
        assert structured.topic == "API design"
        assert structured.stages == ["round_1", "round_2"]
        assert len(structured.participants) == 2
        assert structured.participants[0].engine == "claude"
        assert structured.participants[1].engine == "gemini"
        assert len(structured.utterances) == 4
        assert structured.utterances[0].stage == "round_1"
        assert structured.utterances[2].stage == "round_2"
        assert structured.status == "completed"
        assert structured.completed_at is not None
        assert structured.current_stage == "round_2"

    def test_single_round(self):
        @dataclass
        class FakeSession:
            thread_id: str = "t2"
            channel_id: str = "ch1"
            topic: str = "Quick chat"
            engines: list[str] = field(default_factory=lambda: ["claude"])
            total_rounds: int = 1
            current_round: int = 1
            transcript: list[tuple[str, str]] = field(
                default_factory=lambda: [("claude", "answer")]
            )
            cancel_event: object = field(default_factory=lambda: anyio.Event())
            completed: bool = True

        session = FakeSession()
        structured = StructuredRoundtableStore.from_roundtable_session(session, "proj")
        assert structured.stages == ["round_1"]
        assert len(structured.utterances) == 1
        assert structured.utterances[0].engine == "claude"
