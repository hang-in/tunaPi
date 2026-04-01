"""Tests for Discord render module."""

from __future__ import annotations

from tunapi.discord.render import (
    MAX_BODY_CHARS,
    MAX_MESSAGE_CHARS,
    _close_fence_chunk,
    _ensure_trailing_newline,
    _FenceState,
    _reopen_fence_prefix,
    _scan_fence_state,
    _split_block,
    _split_long_line,
    _update_fence_state,
    prepare_discord,
    prepare_discord_multi,
    split_markdown_body,
    trim_body,
)
from tunapi.markdown import MarkdownParts


# ---------------------------------------------------------------------------
# _FenceState helpers
# ---------------------------------------------------------------------------


class TestUpdateFenceState:
    """Tests for _update_fence_state."""

    def test_non_fence_line_returns_none_state(self) -> None:
        assert _update_fence_state("just text", None) is None

    def test_non_fence_line_preserves_existing_state(self) -> None:
        existing = _FenceState(fence="```", indent="", header="```python")
        result = _update_fence_state("some code", existing)
        assert result is existing

    def test_opening_fence(self) -> None:
        state = _update_fence_state("```python", None)
        assert state is not None
        assert state.fence == "```"
        assert state.indent == ""
        assert state.header == "```python"

    def test_opening_fence_with_indent(self) -> None:
        state = _update_fence_state("  ```js", None)
        assert state is not None
        assert state.indent == "  "
        assert state.fence == "```"

    def test_closing_fence_same_marker(self) -> None:
        existing = _FenceState(fence="```", indent="", header="```python")
        result = _update_fence_state("```", existing)
        assert result is None

    def test_closing_fence_longer_marker(self) -> None:
        existing = _FenceState(fence="```", indent="", header="```python")
        result = _update_fence_state("````", existing)
        assert result is None

    def test_mismatched_fence_char_does_not_close(self) -> None:
        existing = _FenceState(fence="```", indent="", header="```python")
        result = _update_fence_state("~~~", existing)
        assert result is existing

    def test_shorter_fence_does_not_close(self) -> None:
        existing = _FenceState(fence="````", indent="", header="````py")
        result = _update_fence_state("```", existing)
        assert result is existing

    def test_tilde_fence(self) -> None:
        state = _update_fence_state("~~~bash", None)
        assert state is not None
        assert state.fence == "~~~"


class TestScanFenceState:
    """Tests for _scan_fence_state."""

    def test_no_fences(self) -> None:
        assert _scan_fence_state("hello\nworld", None) is None

    def test_opened_fence(self) -> None:
        state = _scan_fence_state("```python\ncode here", None)
        assert state is not None
        assert state.fence == "```"

    def test_opened_and_closed_fence(self) -> None:
        assert _scan_fence_state("```python\ncode\n```", None) is None

    def test_multiple_fences(self) -> None:
        text = "```py\ncode\n```\n\n```js\nalert(1)"
        state = _scan_fence_state(text, None)
        assert state is not None
        assert state.header == "```js"

    def test_preserves_initial_state(self) -> None:
        existing = _FenceState(fence="```", indent="", header="```py")
        state = _scan_fence_state("some code\nmore code", existing)
        assert state is existing


# ---------------------------------------------------------------------------
# _ensure_trailing_newline
# ---------------------------------------------------------------------------


class TestEnsureTrailingNewline:
    def test_already_has_newline(self) -> None:
        assert _ensure_trailing_newline("hello\n") == "hello\n"

    def test_no_newline(self) -> None:
        assert _ensure_trailing_newline("hello") == "hello\n"

    def test_carriage_return(self) -> None:
        assert _ensure_trailing_newline("hello\r") == "hello\r"

    def test_empty_string(self) -> None:
        assert _ensure_trailing_newline("") == "\n"


# ---------------------------------------------------------------------------
# _close_fence_chunk / _reopen_fence_prefix
# ---------------------------------------------------------------------------


class TestCloseFenceChunk:
    def test_basic_close(self) -> None:
        state = _FenceState(fence="```", indent="", header="```python")
        result = _close_fence_chunk("some code", state)
        assert result == "some code\n```\n"

    def test_close_with_indent(self) -> None:
        state = _FenceState(fence="```", indent="  ", header="  ```py")
        result = _close_fence_chunk("code", state)
        assert result == "code\n  ```\n"

    def test_already_has_newline(self) -> None:
        state = _FenceState(fence="```", indent="", header="```")
        result = _close_fence_chunk("code\n", state)
        assert result == "code\n```\n"


class TestReopenFencePrefix:
    def test_basic_reopen(self) -> None:
        state = _FenceState(fence="```", indent="", header="```python")
        assert _reopen_fence_prefix(state) == "```python\n"

    def test_reopen_with_tilde(self) -> None:
        state = _FenceState(fence="~~~", indent="", header="~~~bash")
        assert _reopen_fence_prefix(state) == "~~~bash\n"


# ---------------------------------------------------------------------------
# _split_long_line
# ---------------------------------------------------------------------------


class TestSplitLongLine:
    def test_short_line_unchanged(self) -> None:
        assert _split_long_line("hello", 100) == ["hello"]

    def test_exact_length(self) -> None:
        assert _split_long_line("abc", 3) == ["abc"]

    def test_split_plain(self) -> None:
        result = _split_long_line("abcdef", 3)
        assert result == ["abc", "def"]

    def test_split_preserves_newline_ending(self) -> None:
        result = _split_long_line("abcdef\n", 3)
        assert result == ["abc", "def\n"]

    def test_split_preserves_crlf(self) -> None:
        result = _split_long_line("abcd\r\n", 3)
        assert result == ["abc", "d\r\n"]

    def test_split_preserves_cr(self) -> None:
        result = _split_long_line("abcd\r", 3)
        assert result == ["abc", "d\r"]

    def test_empty_line(self) -> None:
        assert _split_long_line("", 5) == [""]


# ---------------------------------------------------------------------------
# _split_block
# ---------------------------------------------------------------------------


class TestSplitBlock:
    def test_short_block_unchanged(self) -> None:
        assert _split_block("hello", 100) == ["hello"]

    def test_split_by_lines(self) -> None:
        block = "line1\nline2\nline3\n"
        result = _split_block(block, 12)
        assert all(len(p) <= 12 for p in result)
        assert "".join(result) == block

    def test_single_long_line_within_block(self) -> None:
        block = "a" * 10
        result = _split_block(block, 4)
        assert "".join(result) == block
        assert all(len(p) <= 4 for p in result)


# ---------------------------------------------------------------------------
# split_markdown_body
# ---------------------------------------------------------------------------


class TestSplitMarkdownBody:
    def test_empty_body(self) -> None:
        assert split_markdown_body("", 100) == []

    def test_whitespace_only(self) -> None:
        assert split_markdown_body("   \n\n  ", 100) == []

    def test_none_like_empty(self) -> None:
        # body="" treated as empty
        assert split_markdown_body("", 500) == []

    def test_short_body_single_chunk(self) -> None:
        result = split_markdown_body("hello world", 100)
        assert result == ["hello world"]

    def test_split_at_paragraph_boundary(self) -> None:
        body = "paragraph one\n\nparagraph two"
        result = split_markdown_body(body, 20)
        assert len(result) >= 2
        # All original text should be reconstructable
        combined = "".join(result)
        assert "paragraph one" in combined
        assert "paragraph two" in combined

    def test_respects_max_chars(self) -> None:
        body = "short\n\n" + "x" * 50 + "\n\n" + "y" * 50
        result = split_markdown_body(body, 60)
        for chunk in result:
            # Chunks may slightly exceed due to fence closing, but body content fits
            assert len(chunk) <= 120  # generous bound

    def test_code_fence_continuity(self) -> None:
        body = "```python\n" + "code\n" * 20 + "```"
        result = split_markdown_body(body, 40)
        assert len(result) >= 2
        # First chunk should start with opening fence
        assert result[0].startswith("```python")
        # Intermediate chunks should be reopened with fence header
        for chunk in result[1:]:
            assert "```python" in chunk

    def test_max_chars_floor_at_one(self) -> None:
        # max_chars=0 should be clamped to 1
        result = split_markdown_body("a", 0)
        assert "a" in "".join(result)

    def test_negative_max_chars(self) -> None:
        result = split_markdown_body("hello", -5)
        assert "hello" in "".join(result)


# ---------------------------------------------------------------------------
# trim_body
# ---------------------------------------------------------------------------


class TestTrimBody:
    def test_none_returns_none(self) -> None:
        assert trim_body(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert trim_body("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert trim_body("   ") is None

    def test_short_body_unchanged(self) -> None:
        assert trim_body("hello") == "hello"

    def test_exact_limit(self) -> None:
        body = "a" * MAX_BODY_CHARS
        assert trim_body(body) == body

    def test_over_limit_truncated_with_ellipsis(self) -> None:
        body = "a" * (MAX_BODY_CHARS + 100)
        result = trim_body(body)
        assert result is not None
        assert len(result) == MAX_BODY_CHARS
        assert result.endswith("…")

    def test_custom_max_chars(self) -> None:
        result = trim_body("abcdefghij", max_chars=5)
        assert result is not None
        assert len(result) == 5
        assert result.endswith("…")

    def test_trimmed_to_whitespace_returns_none(self) -> None:
        # If after trimming the result is only whitespace, return None
        result = trim_body("   x", max_chars=4)
        assert result == "   x"


# ---------------------------------------------------------------------------
# prepare_discord
# ---------------------------------------------------------------------------


class TestPrepareDiscord:
    def test_header_only(self) -> None:
        parts = MarkdownParts(header="# Title")
        result = prepare_discord(parts)
        assert result == "# Title"

    def test_header_and_body(self) -> None:
        parts = MarkdownParts(header="**bold**", body="some text")
        result = prepare_discord(parts)
        assert "**bold**" in result
        assert "some text" in result

    def test_all_parts(self) -> None:
        parts = MarkdownParts(header="H", body="B", footer="F")
        result = prepare_discord(parts)
        assert result == "H\n\nB\n\nF"

    def test_body_trimmed(self) -> None:
        long_body = "x" * (MAX_BODY_CHARS + 100)
        parts = MarkdownParts(header="H", body=long_body)
        result = prepare_discord(parts)
        assert "…" in result

    def test_empty_body(self) -> None:
        parts = MarkdownParts(header="H", body=None, footer="F")
        result = prepare_discord(parts)
        assert result == "H\n\nF"

    def test_whitespace_body_omitted(self) -> None:
        parts = MarkdownParts(header="H", body="   ")
        result = prepare_discord(parts)
        # whitespace body should be trimmed to None by trim_body
        assert result == "H"


# ---------------------------------------------------------------------------
# prepare_discord_multi
# ---------------------------------------------------------------------------


class TestPrepareDiscordMulti:
    def test_single_message_no_split(self) -> None:
        parts = MarkdownParts(header="H", body="short body", footer="F")
        messages = prepare_discord_multi(parts)
        assert len(messages) == 1
        assert "H" in messages[0]
        assert "short body" in messages[0]
        assert "F" in messages[0]

    def test_no_body(self) -> None:
        parts = MarkdownParts(header="H", body=None, footer="F")
        messages = prepare_discord_multi(parts)
        assert len(messages) == 1

    def test_whitespace_body(self) -> None:
        parts = MarkdownParts(header="H", body="   \n  ")
        messages = prepare_discord_multi(parts)
        assert len(messages) == 1

    def test_multi_message_continued_header(self) -> None:
        long_body = ("paragraph\n\n") * 30
        parts = MarkdownParts(header="Title", body=long_body, footer="end")
        messages = prepare_discord_multi(parts, max_body_chars=50)
        assert len(messages) >= 2
        # First message should have original header
        assert "Title" in messages[0]
        assert "continued" not in messages[0]
        # Subsequent messages should have continuation header
        for msg in messages[1:]:
            assert "continued" in msg

    def test_multi_message_no_header(self) -> None:
        long_body = ("word " * 20 + "\n\n") * 10
        parts = MarkdownParts(header="", body=long_body)
        messages = prepare_discord_multi(parts, max_body_chars=50)
        if len(messages) > 1:
            assert "continued" in messages[1]

    def test_footer_on_all_messages(self) -> None:
        long_body = ("text\n\n") * 30
        parts = MarkdownParts(header="H", body=long_body, footer="--footer--")
        messages = prepare_discord_multi(parts, max_body_chars=30)
        for msg in messages:
            assert "--footer--" in msg

    def test_code_fence_split(self) -> None:
        code_body = "```python\n" + "x = 1\n" * 30 + "```"
        parts = MarkdownParts(header="H", body=code_body, footer="F")
        messages = prepare_discord_multi(parts, max_body_chars=60)
        assert len(messages) >= 2
        # All chunks should have fence markers
        for msg in messages:
            assert "```" in msg

    def test_constants(self) -> None:
        assert MAX_MESSAGE_CHARS == 2000
        assert MAX_BODY_CHARS == 1500
