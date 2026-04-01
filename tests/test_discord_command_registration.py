"""Tests for plugin command registration helpers."""

from __future__ import annotations

from tunapi.discord.commands.registration import _format_plugin_starter_message


class TestFormatPluginStarterMessage:
    def test_no_args(self) -> None:
        assert _format_plugin_starter_message("hello", "", max_chars=2000) == "/hello"

    def test_with_args(self) -> None:
        assert (
            _format_plugin_starter_message("hello", "world", max_chars=2000)
            == "/hello world"
        )

    def test_truncates_with_ellipsis(self) -> None:
        msg = _format_plugin_starter_message("hello", "x" * 100, max_chars=20)
        assert msg.startswith("/hello ")
        assert msg.endswith("…")
        assert len(msg) <= 20
