"""Tests for plugin command registration helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from tunapi.discord.commands.registration import (
    _format_plugin_starter_message,
    discover_command_ids,
)


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


# ---------------------------------------------------------------------------
# Migrated from test_coverage_push.py
# ---------------------------------------------------------------------------


class TestDiscordRegistration:
    def test_discover_command_ids(self, monkeypatch: pytest.MonkeyPatch):
        with patch(
            "tunapi.discord.commands.registration.list_command_ids",
            return_value=["Help", "Model"],
        ):
            ids = discover_command_ids(None)
        assert ids == {"help", "model"}

    def test_discover_with_allowlist(self):
        with patch(
            "tunapi.discord.commands.registration.list_command_ids",
            return_value=["Help"],
        ):
            ids = discover_command_ids({"help"})
        assert ids == {"help"}

    def test_format_starter_short(self):
        assert _format_plugin_starter_message("help", "") == "/help"

    def test_format_starter_with_args(self):
        result = _format_plugin_starter_message("model", "claude opus")
        assert result == "/model claude opus"

    def test_format_starter_truncated(self):
        result = _format_plugin_starter_message("cmd", "x" * 3000, max_chars=20)
        assert len(result) <= 20
        assert result.endswith("…")


class TestRegisterPluginCommands:
    def test_register_skips_missing(self):
        bot = MagicMock()
        bot.bot = MagicMock()
        cfg = MagicMock()
        cfg.runtime.allowlist = None
        with patch(
            "tunapi.discord.commands.registration.get_command",
            return_value=None,
        ):
            from tunapi.discord.commands.registration import register_plugin_commands

            register_plugin_commands(
                bot,
                cfg,
                command_ids={"nonexistent"},
                running_tasks={},
                state_store=MagicMock(),
                prefs_store=MagicMock(),
                default_engine_override=None,
            )
        # No crash, no command registered

    def test_register_truncates_description(self):
        bot = MagicMock()
        mock_pycord_bot = MagicMock()
        bot.bot = mock_pycord_bot
        cfg = MagicMock()
        cfg.runtime.allowlist = None

        backend = MagicMock()
        backend.description = "A" * 200  # Over 100 char limit

        with patch(
            "tunapi.discord.commands.registration.get_command",
            return_value=backend,
        ):
            from tunapi.discord.commands.registration import register_plugin_commands

            register_plugin_commands(
                bot,
                cfg,
                command_ids={"test_cmd"},
                running_tasks={},
                state_store=MagicMock(),
                prefs_store=MagicMock(),
                default_engine_override=None,
            )
        # The slash_command decorator was called
        mock_pycord_bot.slash_command.assert_called_once()
        call_kwargs = mock_pycord_bot.slash_command.call_args
        desc = call_kwargs.kwargs.get("description") or call_kwargs[1].get(
            "description", ""
        )
        assert len(desc) <= 100
