"""Tests for core/rt_utterance.py."""

from __future__ import annotations

from tunapi.core.rt_participant import (
    build_participants_from_engines,
)
from tunapi.core.rt_utterance import (
    transcript_to_utterances,
    utterances_to_transcript,
)


_SAMPLE_TRANSCRIPT: list[tuple[str, str]] = [
    ("claude", "Claude says hello"),
    ("gemini", "Gemini responds"),
]


class TestTranscriptToUtterances:
    def test_basic_conversion(self):
        participants = build_participants_from_engines(["claude", "gemini"])
        utterances = transcript_to_utterances(_SAMPLE_TRANSCRIPT, participants)
        assert len(utterances) == 2
        assert utterances[0].engine == "claude"
        assert utterances[0].output_text == "Claude says hello"
        assert utterances[1].engine == "gemini"
        assert utterances[1].output_text == "Gemini responds"

    def test_stage_assignment_single_round(self):
        participants = build_participants_from_engines(["claude", "gemini"])
        utterances = transcript_to_utterances(_SAMPLE_TRANSCRIPT, participants)
        assert utterances[0].stage == "round_1"
        assert utterances[1].stage == "round_1"

    def test_stage_assignment_multi_round(self):
        transcript = [
            ("claude", "r1 claude"),
            ("gemini", "r1 gemini"),
            ("claude", "r2 claude"),
            ("gemini", "r2 gemini"),
        ]
        participants = build_participants_from_engines(["claude", "gemini"])
        utterances = transcript_to_utterances(transcript, participants)
        assert utterances[0].stage == "round_1"
        assert utterances[1].stage == "round_1"
        assert utterances[2].stage == "round_2"
        assert utterances[3].stage == "round_2"

    def test_participant_id_linked(self):
        participants = build_participants_from_engines(["claude"])
        utterances = transcript_to_utterances([("claude", "answer")], participants)
        assert utterances[0].participant_id == participants[0].participant_id

    def test_role_from_participant(self):
        participants = build_participants_from_engines(["claude"])
        utterances = transcript_to_utterances([("claude", "answer")], participants)
        assert utterances[0].role == "claude"  # from_engines sets role=engine

    def test_reply_to_chain(self):
        participants = build_participants_from_engines(["claude", "gemini"])
        utterances = transcript_to_utterances(_SAMPLE_TRANSCRIPT, participants)
        assert utterances[0].reply_to is None
        assert utterances[1].reply_to == utterances[0].utterance_id

    def test_unique_ids(self):
        participants = build_participants_from_engines(["claude", "gemini"])
        utterances = transcript_to_utterances(_SAMPLE_TRANSCRIPT, participants)
        ids = [u.utterance_id for u in utterances]
        assert len(set(ids)) == len(ids)

    def test_unknown_engine_fallback(self):
        participants = build_participants_from_engines(["claude"])
        utterances = transcript_to_utterances(
            [("unknown_engine", "answer")], participants
        )
        assert utterances[0].engine == "unknown_engine"
        assert utterances[0].participant_id == ""
        assert utterances[0].role == "unknown_engine"

    def test_empty_transcript(self):
        participants = build_participants_from_engines(["claude"])
        utterances = transcript_to_utterances([], participants)
        assert utterances == []

    def test_created_at_set(self):
        participants = build_participants_from_engines(["claude"])
        utterances = transcript_to_utterances([("claude", "hi")], participants)
        assert utterances[0].created_at != ""


class TestUtterancesToTranscript:
    def test_roundtrip(self):
        participants = build_participants_from_engines(["claude", "gemini"])
        utterances = transcript_to_utterances(_SAMPLE_TRANSCRIPT, participants)
        result = utterances_to_transcript(utterances)
        assert result == _SAMPLE_TRANSCRIPT

    def test_empty(self):
        assert utterances_to_transcript([]) == []

    def test_preserves_order(self):
        transcript = [
            ("claude", "first"),
            ("gemini", "second"),
            ("claude", "third"),
        ]
        participants = build_participants_from_engines(["claude", "gemini"])
        utterances = transcript_to_utterances(transcript, participants)
        result = utterances_to_transcript(utterances)
        assert result == transcript
