"""Tests for message header parsing helpers in the loop module."""

from __future__ import annotations

from tunapi.discord.loop_state import _extract_engine_id_from_header


class TestExtractEngineIdFromHeader:
    def test_none(self) -> None:
        assert _extract_engine_id_from_header(None) is None

    def test_empty(self) -> None:
        assert _extract_engine_id_from_header("") is None

    def test_standard_header(self) -> None:
        assert _extract_engine_id_from_header("done · codex · 10s") == "codex"

    def test_header_with_step(self) -> None:
        assert _extract_engine_id_from_header("done · codex · 10s · step 2") == "codex"

    def test_header_without_spaces(self) -> None:
        assert _extract_engine_id_from_header("done·codex·10s") == "codex"

    def test_engine_wrapped_in_backticks(self) -> None:
        assert _extract_engine_id_from_header("done · `codex` · 10s") == "codex"

    def test_no_separator(self) -> None:
        assert _extract_engine_id_from_header("not a status line") is None
