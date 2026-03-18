"""Tests for Mattermost rendering helpers."""

from __future__ import annotations

from tunapi.markdown import MarkdownParts
from tunapi.mattermost.render import (
    prepare_mattermost,
    prepare_mattermost_multi,
    split_markdown_body,
    trim_body,
)


class TestTrimBody:
    def test_none_input(self):
        assert trim_body(None) is None

    def test_empty_input(self):
        assert trim_body("") is None
        assert trim_body("   ") is None

    def test_short_body(self):
        assert trim_body("hello") == "hello"

    def test_long_body_truncated(self):
        body = "x" * 100
        result = trim_body(body, max_chars=50)
        assert len(result) == 50
        assert result.endswith("…")


class TestSplitMarkdownBody:
    def test_short_body(self):
        assert split_markdown_body("hello", 100) == ["hello"]

    def test_splits_at_paragraphs(self):
        body = "para1\n\npara2\n\npara3"
        chunks = split_markdown_body(body, 12)
        assert len(chunks) >= 2
        assert "para1" in chunks[0]

    def test_single_paragraph_too_long(self):
        body = "x" * 200
        chunks = split_markdown_body(body, 100)
        assert len(chunks) >= 1


class TestPrepareMattermost:
    def test_simple(self):
        parts = MarkdownParts(header="**working**", body="step 1")
        text = prepare_mattermost(parts)
        assert "**working**" in text
        assert "step 1" in text

    def test_with_footer(self):
        parts = MarkdownParts(header="h", body="b", footer="`resume cmd`")
        text = prepare_mattermost(parts)
        assert "`resume cmd`" in text


class TestPrepareMattermostMulti:
    def test_short_message_single(self):
        parts = MarkdownParts(header="h", body="short")
        messages = prepare_mattermost_multi(parts)
        assert len(messages) == 1

    def test_long_message_splits(self):
        body = "\n\n".join(f"paragraph {i}" for i in range(50))
        parts = MarkdownParts(header="header", body=body, footer="footer")
        messages = prepare_mattermost_multi(parts, max_body_chars=100)
        assert len(messages) > 1
        assert "header" in messages[0]
        assert "footer" in messages[-1]
        assert "continued" in messages[1]
