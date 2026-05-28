import re
from unittest.mock import MagicMock


from tunapi.telegram.render import render_markdown, split_markdown_body
from tunapi.telegram.onboarding import (
    append_dialogue,
    render_workspace_preview,
    render_assistant_preview,
    render_handoff_preview,
    render_persona_tabs,
    render_botfather_instructions,
    render_private_chat_instructions,
    render_topics_group_instructions,
    render_generic_capture_prompt,
    render_topics_validation_warning,
    render_config_malformed_warning,
    render_backup_failed_warning,
    format_bool,
    render_engine_table,
    render_persona_preview,
)


def test_render_markdown_basic_entities() -> None:
    text, entities = render_markdown("**bold** and `code`")

    assert text == "bold and code\n\n"
    assert entities == [
        {"type": "bold", "offset": 0, "length": 4},
        {"type": "code", "offset": 9, "length": 4},
    ]


def test_render_markdown_code_fence_language_is_string() -> None:
    text, entities = render_markdown("```py\nprint('x')\n```")

    assert text == "print('x')\n\n"
    assert entities is not None
    assert any(e.get("type") == "pre" and e.get("language") == "py" for e in entities)
    assert any(e.get("type") == "code" for e in entities)


def test_render_markdown_drops_local_text_links() -> None:
    text, entities = render_markdown("[/tmp/file.py#L12](/tmp/file.py#L12)")

    assert "/tmp/file.py#L12" in text
    assert not any(e.get("type") == "text_link" for e in entities)


def test_render_markdown_keeps_https_text_links() -> None:
    _, entities = render_markdown("[docs](https://example.com/path)")

    assert any(
        e.get("type") == "text_link" and e.get("url") == "https://example.com/path"
        for e in entities
    )


def test_render_markdown_keeps_ordered_numbering_with_unindented_sub_bullets() -> None:
    md = (
        "1. Tune maker\n"
        "- Sweep\n"
        "- Keep data\n"
        "1. Increase\n"
        "- Raise target\n"
        "- Keep\n"
        "1. Train\n"
        "- Start\n"
        "1. Add\n"
        "- Keep exposure\n"
        "1. Run\n"
        "- Target pnl\n"
    )

    text, _ = render_markdown(md)
    numbered = [line for line in text.splitlines() if re.match(r"^\d+\.\s", line)]

    assert numbered == [
        "1. Tune maker",
        "2. Increase",
        "3. Train",
        "4. Add",
        "5. Run",
    ]


def test_split_markdown_body_closes_and_reopens_fence() -> None:
    body = "```py\n" + ("line\n" * 10) + "```\n\npost"

    chunks = split_markdown_body(body, max_chars=40)

    assert len(chunks) > 1
    assert chunks[0].rstrip().endswith("```")
    assert chunks[1].startswith("```py\n")


class TestRenderHelpers:
    def test_append_dialogue(self):
        from rich.text import Text

        t = Text()
        append_dialogue(t, "bot", "hi", speaker_style="bold")
        assert "bot" in t.plain
        assert "hi" in t.plain

    def test_render_previews(self):
        """Smoke test: all render functions produce non-empty Text."""
        assert render_workspace_preview().plain
        assert render_assistant_preview().plain
        assert render_handoff_preview().plain
        assert render_persona_tabs() is not None
        assert render_botfather_instructions().plain
        assert render_private_chat_instructions("@bot").plain
        assert render_topics_group_instructions("@bot").plain
        assert render_generic_capture_prompt("@bot").plain

    def test_render_warnings(self):
        from tunapi.config import ConfigError

        w = render_topics_validation_warning(ConfigError("oops"))
        assert "oops" in w.plain
        w2 = render_config_malformed_warning(ConfigError("bad"))
        assert "bad" in w2.plain
        w3 = render_backup_failed_warning(OSError("disk"))
        assert "disk" in w3.plain


class TestFormatBool:
    def test_none(self):
        assert format_bool(None) == "n/a"

    def test_true(self):
        assert format_bool(True) == "yes"

    def test_false(self):
        assert format_bool(False) == "no"


class TestRenderEngineTable:
    def test_renders(self):
        ui = MagicMock()
        ui.print = MagicMock()
        rows = [("claude", True, None), ("codex", False, "npm i codex")]
        render_engine_table(ui, rows)
        ui.print.assert_called_once()

    def test_empty(self):
        ui = MagicMock()
        ui.print = MagicMock()
        render_engine_table(ui, [])
        ui.print.assert_called_once()


class TestRenderPersonaPreview:
    def test_renders(self):
        ui = MagicMock()
        ui.print = MagicMock()
        render_persona_preview(ui)
        ui.print.assert_called()
