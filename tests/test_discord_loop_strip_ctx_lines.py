"""Tests for stripping ctx lines from bot messages."""

from __future__ import annotations

from tunapi.discord.loop_state import _strip_ctx_lines


class TestStripCtxLines:
    def test_none(self) -> None:
        assert _strip_ctx_lines(None) is None

    def test_empty(self) -> None:
        assert _strip_ctx_lines("") is None

    def test_removes_plain_ctx_line(self) -> None:
        text = "hello\nctx: takopi @main\nworld"
        assert _strip_ctx_lines(text) == "hello\nworld"

    def test_removes_backticked_ctx_line(self) -> None:
        text = "hello\n`ctx: takopi @main`\nworld"
        assert _strip_ctx_lines(text) == "hello\nworld"

    def test_returns_none_if_only_ctx(self) -> None:
        assert _strip_ctx_lines("`ctx: takopi @main`") is None
