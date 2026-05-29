"""Tests for the pure roundtable role helpers (core/roundtable/roles.py)."""

from __future__ import annotations

import pytest

from tunapi.core.roundtable import roles


class TestCanonicalRole:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("proposer", "proposer"),
            ("Reviewer", "reviewer"),
            ("critic", "reviewer"),
            (" JUDGE ", "verifier"),
            ("lead", "synthesizer"),
            ("verifier", "verifier"),
        ],
    )
    def test_aliases_and_normalization(self, raw, expected):
        assert roles.canonical_role(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "moderator", "xyz"])
    def test_unknown_or_empty_returns_none(self, raw):
        assert roles.canonical_role(raw) is None


class TestRoleGuidance:
    @pytest.mark.parametrize("role", ["proposer", "reviewer", "verifier", "synthesizer"])
    def test_known_roles_have_nonempty_guidance(self, role):
        assert roles.role_guidance(role).strip()

    def test_aliases_share_guidance(self):
        assert roles.role_guidance("critic") == roles.role_guidance("reviewer")
        assert roles.role_guidance("judge") == roles.role_guidance("verifier")

    def test_synthesizer_directive_mentions_sections(self):
        g = roles.role_guidance("synthesizer")
        assert "## Consensus" in g and "## Disagreements" in g

    @pytest.mark.parametrize("role", [None, "", "moderator"])
    def test_unknown_returns_empty(self, role):
        assert roles.role_guidance(role) == ""


class TestEffectiveMaxTokens:
    @pytest.mark.parametrize(
        ("role", "cap"),
        [
            ("proposer", 1200),
            ("reviewer", 900),
            ("critic", 900),
            ("verifier", 800),
            ("synthesizer", 2000),
        ],
    )
    def test_role_caps(self, role, cap):
        assert roles.effective_max_tokens(role) == cap

    def test_override_wins(self):
        assert roles.effective_max_tokens("proposer", override=50) == 50
        assert roles.effective_max_tokens(None, override=77) == 77

    def test_unknown_role_none(self):
        assert roles.effective_max_tokens("moderator") is None
        assert roles.effective_max_tokens(None) is None

    def test_constants_match_dict(self):
        assert roles.PROPOSER_MAX_TOKENS == 1200
        assert roles.SYNTHESIZER_MAX_TOKENS == 2000
