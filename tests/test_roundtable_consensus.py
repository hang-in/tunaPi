"""Tests for the pure consensus/synthesis parser (core/roundtable/consensus.py)."""

from __future__ import annotations

from tunapi.core.roundtable.consensus import ExtractedSynthesis, extract_synthesis


class TestNoStructure:
    def test_empty_returns_none(self):
        assert extract_synthesis("") is None
        assert extract_synthesis("   \n  ") is None

    def test_plain_prose_returns_none(self):
        assert extract_synthesis("Just a paragraph with no headers or marker.") is None


class TestMarkdownSections:
    def test_parses_sections_and_bullets(self):
        text = (
            "## Consensus\n- agree A\n- agree B\n\n"
            "## Disagreements\n* contested X\n\n"
            "## Open questions\n1. what about Y?\n2) and Z?\n\n"
            "## Recommendation\nGo with A."
        )
        out = extract_synthesis(text)
        assert isinstance(out, ExtractedSynthesis)
        assert out.agreements == ["agree A", "agree B"]
        assert out.disagreements == ["contested X"]
        assert out.open_questions == ["what about Y?", "and Z?"]
        assert out.thesis == "Go with A."

    def test_disagreement_not_swallowed_by_agreement(self):
        # 'disagreements' contains 'agreements' — must classify as disagreements.
        out = extract_synthesis("## Disagreements\n- d1")
        assert out is not None
        assert out.disagreements == ["d1"]
        assert out.agreements == []

    def test_unrecognized_headers_ignored(self):
        out = extract_synthesis("## Intro\n- noise\n\n## Consensus\n- real")
        assert out is not None
        assert out.agreements == ["real"]


class TestMarker:
    def test_marker_takes_priority(self):
        text = (
            '<!-- consensus {"recommendation": "ship it", '
            '"agreements": ["a1"], "disagreements": [], "open_questions": ["q1"]} -->\n'
            "## Consensus\n- should be ignored"
        )
        out = extract_synthesis(text)
        assert out is not None
        assert out.thesis == "ship it"
        assert out.agreements == ["a1"]
        assert out.open_questions == ["q1"]

    def test_marker_invalid_json_falls_back_to_markdown(self):
        text = "<!-- consensus {not json} -->\n## Consensus\n- a1"
        out = extract_synthesis(text)
        assert out is not None
        assert out.agreements == ["a1"]

    def test_marker_non_string_items_skipped(self):
        text = '<!-- consensus {"agreements": ["ok", 5, null]} -->'
        out = extract_synthesis(text)
        assert out is not None
        assert out.agreements == ["ok"]


class TestExtractFromTranscript:
    def test_scans_from_end_for_structured_answer(self):
        from tunapi.core.memory_facade import _extract_from_transcript

        transcript = [
            ["claude", "plain opinion, no structure"],
            ["codex", "## Consensus\n- final agreement"],
        ]
        out = _extract_from_transcript(transcript)
        assert out is not None
        assert out.agreements == ["final agreement"]

    def test_no_structured_answer_returns_none(self):
        from tunapi.core.memory_facade import _extract_from_transcript

        assert _extract_from_transcript([["a", "x"], ["b", "y"]]) is None
