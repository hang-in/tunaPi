"""Tests for handlers module."""

from tunapi.discord.handlers import _format_engine_starter_message, parse_branch_prefix


class TestParseBranchPrefix:
    """Tests for parse_branch_prefix function."""

    def test_no_prefix(self) -> None:
        """Regular message without @ prefix."""
        branch, prompt = parse_branch_prefix("hello world")
        assert branch is None
        assert prompt == "hello world"

    def test_simple_branch(self) -> None:
        """Message with simple branch prefix."""
        branch, prompt = parse_branch_prefix("@main fix the bug")
        assert branch == "main"
        assert prompt == "fix the bug"

    def test_branch_with_slash(self) -> None:
        """Message with branch containing slash."""
        branch, prompt = parse_branch_prefix("@chore/hello fix something")
        assert branch == "chore/hello"
        assert prompt == "fix something"

    def test_branch_with_hyphen(self) -> None:
        """Message with branch containing hyphen."""
        branch, prompt = parse_branch_prefix("@feat-login add tests")
        assert branch == "feat-login"
        assert prompt == "add tests"

    def test_branch_only(self) -> None:
        """Message with only branch, no prompt."""
        branch, prompt = parse_branch_prefix("@feature/new-thing")
        assert branch == "feature/new-thing"
        assert prompt == ""

    def test_branch_with_extra_spaces(self) -> None:
        """Message with extra whitespace."""
        branch, prompt = parse_branch_prefix("  @main   fix the bug  ")
        assert branch == "main"
        assert prompt == "fix the bug"

    def test_at_symbol_in_middle(self) -> None:
        """@ in middle of message is not a branch prefix."""
        branch, prompt = parse_branch_prefix("email me @user please")
        assert branch is None
        assert prompt == "email me @user please"

    def test_empty_string(self) -> None:
        """Empty string returns None branch."""
        branch, prompt = parse_branch_prefix("")
        assert branch is None
        assert prompt == ""

    def test_just_at_symbol(self) -> None:
        """Just @ returns None branch."""
        branch, prompt = parse_branch_prefix("@")
        assert branch is None
        assert prompt == "@"

    def test_complex_branch_name(self) -> None:
        """Branch with multiple slashes and hyphens."""
        branch, prompt = parse_branch_prefix("@issue-123/fix-bug/v2 do the thing")
        assert branch == "issue-123/fix-bug/v2"
        assert prompt == "do the thing"


class TestFormatEngineStarterMessage:
    def test_short_prompt(self) -> None:
        assert (
            _format_engine_starter_message("codex", "hi", max_chars=2000) == "/codex hi"
        )

    def test_truncates_with_ellipsis(self) -> None:
        msg = _format_engine_starter_message("codex", "x" * 100, max_chars=20)
        assert msg.startswith("/codex ")
        assert msg.endswith("…")
        assert len(msg) <= 20
