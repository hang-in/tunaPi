"""Tests for core/rt_participant.py."""

from __future__ import annotations

from tunapi.core.rt_participant import (
    build_participants_from_config,
    build_participants_from_engines,
)


class TestBuildFromEngines:
    def test_creates_one_per_engine(self):
        participants = build_participants_from_engines(["claude", "gemini"])
        assert len(participants) == 2

    def test_engine_equals_role(self):
        participants = build_participants_from_engines(["claude"])
        assert participants[0].engine == "claude"
        assert participants[0].role == "claude"

    def test_order_is_sequential(self):
        participants = build_participants_from_engines(["a", "b", "c"])
        assert [p.order for p in participants] == [0, 1, 2]

    def test_default_fields(self):
        p = build_participants_from_engines(["claude"])[0]
        assert p.instruction == ""
        assert p.enabled is True
        assert p.participant_id  # non-empty

    def test_unique_ids(self):
        participants = build_participants_from_engines(["claude", "claude"])
        assert participants[0].participant_id != participants[1].participant_id


class TestBuildFromConfig:
    def test_basic_config(self):
        config = [
            {"engine": "claude", "role": "architect", "instruction": "Design APIs"},
            {"engine": "gemini", "role": "critic"},
        ]
        participants = build_participants_from_config(config)
        assert len(participants) == 2
        assert participants[0].engine == "claude"
        assert participants[0].role == "architect"
        assert participants[0].instruction == "Design APIs"
        assert participants[1].engine == "gemini"
        assert participants[1].role == "critic"
        assert participants[1].instruction == ""

    def test_same_engine_different_roles(self):
        config = [
            {"engine": "claude", "role": "architect"},
            {"engine": "claude", "role": "implementer"},
        ]
        participants = build_participants_from_config(config)
        assert len(participants) == 2
        assert participants[0].role == "architect"
        assert participants[1].role == "implementer"
        assert participants[0].engine == participants[1].engine == "claude"
        assert participants[0].participant_id != participants[1].participant_id

    def test_default_role_from_engine(self):
        config = [{"engine": "codex"}]
        participants = build_participants_from_config(config)
        assert participants[0].role == "codex"

    def test_explicit_order(self):
        config = [
            {"engine": "claude", "role": "a", "order": 5},
            {"engine": "gemini", "role": "b", "order": 1},
        ]
        participants = build_participants_from_config(config)
        assert participants[0].order == 5
        assert participants[1].order == 1

    def test_disabled_participant(self):
        config = [{"engine": "claude", "role": "x", "enabled": False}]
        participants = build_participants_from_config(config)
        assert participants[0].enabled is False

    def test_auto_order_when_omitted(self):
        config = [
            {"engine": "a", "role": "r1"},
            {"engine": "b", "role": "r2"},
            {"engine": "c", "role": "r3"},
        ]
        participants = build_participants_from_config(config)
        assert [p.order for p in participants] == [0, 1, 2]
