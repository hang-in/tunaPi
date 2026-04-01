"""Combined coverage tests for medium-sized uncovered modules.

Targets:
- runner_bridge.py: pure helpers + send/finalize logic
- cli/config.py: CLI config commands (config_path, list, get, set, unset)
- slack/render.py: mrkdwn rendering functions
- telegram/onboarding.py: ChatInfo, mask_token, check_setup, OnboardingState
- telegram/loop_state.py: stateless helpers (chat_session_key, classify_message, etc.)
- discord/commands/registration.py: discover_command_ids, _format_plugin_starter_message
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

# ---------------------------------------------------------------------------
# 1. runner_bridge.py
# ---------------------------------------------------------------------------
from tunapi.model import (
    Action,
    ActionEvent,
    CompletedEvent,
    ResumeToken,
    StartedEvent,
)
from tunapi.progress import ProgressState, ProgressTracker
from tunapi.runner_bridge import (
    ExecBridgeConfig,
    IncomingMessage,
    ProgressEdits,
    ProgressMessageState,
    RunOutcome,
    RunningTask,
    _finalize_run,
    _flatten_exception_group,
    _format_error,
    _strip_resume_lines,
    send_initial_progress,
    send_result_message,
    sync_resume_token,
)
from tunapi.transport import MessageRef, RenderedMessage


# -- _strip_resume_lines --

def test_strip_resume_lines_filters() -> None:
    text = "resume:abc\nhello world\nresume:def"
    result = _strip_resume_lines(text, is_resume_line=lambda l: l.startswith("resume:"))
    assert result == "hello world"


def test_strip_resume_lines_all_removed() -> None:
    text = "resume:abc\nresume:def"
    result = _strip_resume_lines(text, is_resume_line=lambda l: l.startswith("resume:"))
    assert result == "continue"


def test_strip_resume_lines_none_removed() -> None:
    text = "hello\nworld"
    result = _strip_resume_lines(text, is_resume_line=lambda _: False)
    assert result == "hello\nworld"


# -- _flatten_exception_group --

def test_flatten_single_exception() -> None:
    exc = ValueError("test")
    assert _flatten_exception_group(exc) == [exc]


def test_flatten_exception_group() -> None:
    inner1 = ValueError("a")
    inner2 = TypeError("b")
    group = BaseExceptionGroup("g", [inner1, inner2])
    result = _flatten_exception_group(group)
    assert result == [inner1, inner2]


def test_flatten_nested_exception_group() -> None:
    inner = ValueError("deep")
    nested = BaseExceptionGroup("inner", [inner])
    outer = BaseExceptionGroup("outer", [nested, TypeError("flat")])
    result = _flatten_exception_group(outer)
    assert len(result) == 2
    assert result[0] is inner


# -- _format_error --

@pytest.mark.anyio
async def test_format_error_single() -> None:
    exc = ValueError("bad input")
    assert _format_error(exc) == "bad input"


@pytest.mark.anyio
async def test_format_error_empty_message() -> None:
    exc = ValueError()
    assert _format_error(exc) == "ValueError"


@pytest.mark.anyio
async def test_format_error_group_single_after_cancel_filter() -> None:
    cancel_cls = anyio.get_cancelled_exc_class()
    inner = ValueError("real error")
    cancel = cancel_cls()
    group = BaseExceptionGroup("g", [cancel, inner])
    # ExceptionGroup filters out cancel exceptions, leaving just one
    result = _format_error(ExceptionGroup("g", [inner]))
    assert result == "real error"


@pytest.mark.anyio
async def test_format_error_multiple_messages() -> None:
    inner1 = ValueError("err1")
    inner2 = TypeError("err2")
    group = ExceptionGroup("g", [inner1, inner2])
    result = _format_error(group)
    assert "err1" in result
    assert "err2" in result


# -- sync_resume_token --

def test_sync_resume_token_uses_resume_arg() -> None:
    tracker = ProgressTracker(engine="test")
    token = ResumeToken(engine="claude", value="abc")
    result = sync_resume_token(tracker, token)
    assert result is token
    assert tracker.resume is token


def test_sync_resume_token_falls_back_to_tracker() -> None:
    tracker = ProgressTracker(engine="test")
    existing = ResumeToken(engine="claude", value="existing")
    tracker.resume = existing
    result = sync_resume_token(tracker, None)
    assert result is existing


# -- RunOutcome defaults --

def test_run_outcome_defaults() -> None:
    o = RunOutcome()
    assert o.cancelled is False
    assert o.completed is None
    assert o.resume is None


# -- IncomingMessage --

def test_incoming_message() -> None:
    msg = IncomingMessage(
        channel_id=123, message_id=456, text="hello", thread_id=789
    )
    assert msg.channel_id == 123
    assert msg.text == "hello"
    assert msg.thread_id == 789
    assert msg.reply_to is None


# -- ExecBridgeConfig --

def test_exec_bridge_config() -> None:
    transport = MagicMock()
    presenter = MagicMock()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=presenter, final_notify=True
    )
    assert cfg.final_notify is True
    assert cfg.engine_meta is None


# -- ProgressMessageState --

def test_progress_message_state() -> None:
    ref = MessageRef(channel_id=1, message_id=2)
    rendered = RenderedMessage(text="hi")
    state = ProgressMessageState(ref=ref, last_rendered=rendered)
    assert state.ref is ref
    assert state.last_rendered is rendered


# -- send_initial_progress --

@pytest.mark.anyio
async def test_send_initial_progress_sends_message() -> None:
    transport = AsyncMock()
    ref = MessageRef(channel_id=1, message_id=10)
    transport.send.return_value = ref
    transport.edit.return_value = None

    presenter = MagicMock()
    rendered = RenderedMessage(text="progress")
    presenter.render_progress.return_value = rendered

    cfg = ExecBridgeConfig(
        transport=transport, presenter=presenter, final_notify=False
    )
    tracker = ProgressTracker(engine="test")
    reply_to = MessageRef(channel_id=1, message_id=5)

    result = await send_initial_progress(
        cfg,
        channel_id=1,
        reply_to=reply_to,
        label="starting",
        tracker=tracker,
    )
    assert result.ref is ref
    assert result.last_rendered is rendered
    transport.send.assert_awaited_once()


@pytest.mark.anyio
async def test_send_initial_progress_edit_existing() -> None:
    transport = AsyncMock()
    existing_ref = MessageRef(channel_id=1, message_id=20)
    transport.edit.return_value = existing_ref

    presenter = MagicMock()
    rendered = RenderedMessage(text="progress")
    presenter.render_progress.return_value = rendered

    cfg = ExecBridgeConfig(
        transport=transport, presenter=presenter, final_notify=False
    )
    tracker = ProgressTracker(engine="test")
    reply_to = MessageRef(channel_id=1, message_id=5)

    result = await send_initial_progress(
        cfg,
        channel_id=1,
        reply_to=reply_to,
        label="starting",
        tracker=tracker,
        progress_ref=existing_ref,
    )
    assert result.ref is existing_ref
    transport.edit.assert_awaited_once()


@pytest.mark.anyio
async def test_send_initial_progress_send_returns_none() -> None:
    transport = AsyncMock()
    transport.send.return_value = None
    transport.edit.return_value = None

    presenter = MagicMock()
    presenter.render_progress.return_value = RenderedMessage(text="p")

    cfg = ExecBridgeConfig(
        transport=transport, presenter=presenter, final_notify=False
    )
    tracker = ProgressTracker(engine="test")
    reply_to = MessageRef(channel_id=1, message_id=5)

    result = await send_initial_progress(
        cfg, channel_id=1, reply_to=reply_to, label="s", tracker=tracker
    )
    assert result.ref is None
    assert result.last_rendered is None


# -- send_result_message --

@pytest.mark.anyio
async def test_send_result_message_deletes_progress() -> None:
    transport = AsyncMock()
    sent_ref = MessageRef(channel_id=1, message_id=100)
    transport.send.return_value = sent_ref
    transport.edit.return_value = None
    transport.delete.return_value = True

    cfg = ExecBridgeConfig(
        transport=transport, presenter=MagicMock(), final_notify=False
    )
    progress_ref = MessageRef(channel_id=1, message_id=50)
    reply_to = MessageRef(channel_id=1, message_id=5)

    await send_result_message(
        cfg,
        channel_id=1,
        reply_to=reply_to,
        progress_ref=progress_ref,
        message=RenderedMessage(text="done"),
        notify=True,
        edit_ref=None,
    )
    transport.delete.assert_awaited_once()


@pytest.mark.anyio
async def test_send_result_message_no_delete_when_edited() -> None:
    transport = AsyncMock()
    progress_ref = MessageRef(channel_id=1, message_id=50)
    transport.edit.return_value = progress_ref
    transport.delete.return_value = True

    cfg = ExecBridgeConfig(
        transport=transport, presenter=MagicMock(), final_notify=False
    )
    reply_to = MessageRef(channel_id=1, message_id=5)

    await send_result_message(
        cfg,
        channel_id=1,
        reply_to=reply_to,
        progress_ref=progress_ref,
        message=RenderedMessage(text="done"),
        notify=True,
        edit_ref=progress_ref,
    )
    transport.delete.assert_not_awaited()


# -- _finalize_run --

@pytest.mark.anyio
async def test_finalize_run_writes_journal_entries() -> None:
    journal = AsyncMock()
    journal.append = AsyncMock()
    tracker = ProgressTracker(engine="test")
    token = ResumeToken(engine="claude", value="abc")
    tracker.set_resume(token)

    incoming = IncomingMessage(channel_id=1, message_id=2, text="hello world")
    await _finalize_run(
        journal,
        "run-001",
        incoming,
        "claude",
        tracker,
        event="completed",
        data={"ok": True},
    )
    # At least prompt + started + completed entries
    assert journal.append.await_count >= 3


@pytest.mark.anyio
async def test_finalize_run_none_journal() -> None:
    tracker = ProgressTracker(engine="test")
    incoming = IncomingMessage(channel_id=1, message_id=2, text="hi")
    # Should not raise
    await _finalize_run(None, None, incoming, "claude", tracker, event="completed")


@pytest.mark.anyio
async def test_finalize_run_completes_ledger() -> None:
    ledger = AsyncMock()
    ledger.complete = AsyncMock()
    tracker = ProgressTracker(engine="test")
    incoming = IncomingMessage(channel_id=1, message_id=2, text="hi")

    await _finalize_run(
        None, "run-001", incoming, "claude", tracker,
        event="completed", ledger=ledger,
    )
    ledger.complete.assert_awaited_once_with("run-001")


# -- ProgressEdits.on_event --

@pytest.mark.anyio
async def test_progress_edits_on_event_no_ref() -> None:
    """When progress_ref is None, on_event should not increment event_seq."""
    transport = AsyncMock()
    presenter = MagicMock()
    tracker = ProgressTracker(engine="test")

    edits = ProgressEdits(
        transport=transport,
        presenter=presenter,
        channel_id=1,
        progress_ref=None,
        tracker=tracker,
        started_at=0.0,
        clock=time.monotonic,
        last_rendered=None,
    )
    evt = StartedEvent(engine="claude", resume=ResumeToken(engine="claude", value="t"))
    await edits.on_event(evt)
    # event_seq stays 0 because progress_ref is None
    assert edits.event_seq == 0


@pytest.mark.anyio
async def test_progress_edits_on_event_with_ref() -> None:
    transport = AsyncMock()
    presenter = MagicMock()
    tracker = ProgressTracker(engine="test")

    ref = MessageRef(channel_id=1, message_id=10)
    edits = ProgressEdits(
        transport=transport,
        presenter=presenter,
        channel_id=1,
        progress_ref=ref,
        tracker=tracker,
        started_at=0.0,
        clock=time.monotonic,
        last_rendered=None,
    )
    evt = StartedEvent(engine="claude", resume=ResumeToken(engine="claude", value="t"))
    await edits.on_event(evt)
    assert edits.event_seq == 1


@pytest.mark.anyio
async def test_progress_edits_run_returns_when_no_ref() -> None:
    transport = AsyncMock()
    presenter = MagicMock()
    tracker = ProgressTracker(engine="test")

    edits = ProgressEdits(
        transport=transport,
        presenter=presenter,
        channel_id=1,
        progress_ref=None,
        tracker=tracker,
        started_at=0.0,
        clock=time.monotonic,
        last_rendered=None,
    )
    # Should return immediately
    await edits.run()


# ---------------------------------------------------------------------------
# 2. cli/config.py
# ---------------------------------------------------------------------------
from tunapi.cli.config import (
    _config_path_display,
    _exit_config_error,
    _fail_missing_config,
    _flatten_config,
    _load_config_or_exit,
    _normalized_value_from_settings,
    _parse_key_path,
    _parse_value,
    _resolve_config_path_override,
    _resolve_home_config_path,
    _toml_literal,
    config_get,
    config_list,
    config_path_cmd,
    config_set,
    config_unset,
)
from tunapi.config import ConfigError


# -- _config_path_display --

def test_config_path_display_home_relative() -> None:
    home = Path.home()
    path = home / ".tunapi" / "tunapi.toml"
    result = _config_path_display(path)
    assert result == "~/.tunapi/tunapi.toml"


def test_config_path_display_absolute() -> None:
    path = Path("/etc/tunapi/tunapi.toml")
    result = _config_path_display(path)
    assert result == "/etc/tunapi/tunapi.toml"


# -- _fail_missing_config --

def test_fail_missing_config_exists(tmp_path: Path, capsys) -> None:
    p = tmp_path / "tunapi.toml"
    p.write_text("invalid")
    _fail_missing_config(p)
    captured = capsys.readouterr()
    assert "invalid tunapi config" in captured.err


def test_fail_missing_config_not_exists(tmp_path: Path, capsys) -> None:
    p = tmp_path / "missing.toml"
    _fail_missing_config(p)
    captured = capsys.readouterr()
    assert "missing tunapi config" in captured.err


# -- _resolve_config_path_override --

def test_resolve_config_path_override_none() -> None:
    result = _resolve_config_path_override(None)
    assert isinstance(result, Path)


def test_resolve_config_path_override_value() -> None:
    result = _resolve_config_path_override(Path("~/custom.toml"))
    assert str(result).endswith("custom.toml")
    assert "~" not in str(result)


# -- _resolve_home_config_path --

def test_resolve_home_config_path_override(monkeypatch) -> None:
    import tunapi.cli.config as config_mod
    import sys

    fake_module = MagicMock()
    fake_module.HOME_CONFIG_PATH = "/tmp/override.toml"
    monkeypatch.setitem(sys.modules, "tunapi.cli", fake_module)
    result = _resolve_home_config_path()
    assert result == Path("/tmp/override.toml")


def test_resolve_home_config_path_default(monkeypatch) -> None:
    import sys
    monkeypatch.delitem(sys.modules, "tunapi.cli", raising=False)
    result = _resolve_home_config_path()
    from tunapi.config import HOME_CONFIG_PATH
    assert result == HOME_CONFIG_PATH


# -- _exit_config_error --

def test_exit_config_error_raises(capsys) -> None:
    import typer
    with pytest.raises(typer.Exit) as exc_info:
        _exit_config_error(ConfigError("boom"), code=3)
    assert exc_info.value.exit_code == 3


# -- _parse_key_path (additional edge cases beyond test_cli_helpers) --

def test_parse_key_path_empty() -> None:
    with pytest.raises(ConfigError, match="non-empty"):
        _parse_key_path("")


def test_parse_key_path_whitespace_only() -> None:
    with pytest.raises(ConfigError, match="non-empty"):
        _parse_key_path("   ")


def test_parse_key_path_special_chars() -> None:
    with pytest.raises(ConfigError, match="Invalid key segment"):
        _parse_key_path("foo.bar!baz")


def test_parse_key_path_single() -> None:
    assert _parse_key_path("transport") == ["transport"]


def test_parse_key_path_with_hyphens() -> None:
    assert _parse_key_path("some-key.sub-key") == ["some-key", "sub-key"]


# -- _parse_value (additional) --

def test_parse_value_empty() -> None:
    assert _parse_value("") == ""
    assert _parse_value("   ") == ""


def test_parse_value_list() -> None:
    result = _parse_value("[1, 2, 3]")
    assert result == [1, 2, 3]


def test_parse_value_string_fallback() -> None:
    result = _parse_value("hello world")
    assert result == "hello world"


# -- _toml_literal (additional) --

def test_toml_literal_bool() -> None:
    assert _toml_literal(True) == "true"
    assert _toml_literal(False) == "false"


def test_toml_literal_int() -> None:
    assert _toml_literal(42) == "42"


# -- _flatten_config (additional) --

def test_flatten_config_empty() -> None:
    assert _flatten_config({}) == []


def test_flatten_config_nested() -> None:
    config = {"a": {"b": {"c": 1}}, "d": 2}
    result = _flatten_config(config)
    assert ("a.b.c", 1) in result
    assert ("d", 2) in result


# -- _normalized_value_from_settings (additional) --

def test_normalized_value_missing_key() -> None:
    from tunapi.settings import TunapiSettings
    settings = TunapiSettings.model_validate({
        "transport": "telegram",
        "transports": {"telegram": {"bot_token": "t", "chat_id": 1}},
    })
    from tunapi.cli.config import _MISSING
    result = _normalized_value_from_settings(settings, ["nonexistent_key"])
    assert result is _MISSING


def test_normalized_value_deep_missing() -> None:
    from tunapi.settings import TunapiSettings
    settings = TunapiSettings.model_validate({
        "transport": "telegram",
        "transports": {"telegram": {"bot_token": "t", "chat_id": 1}},
    })
    from tunapi.cli.config import _MISSING
    result = _normalized_value_from_settings(settings, ["transports", "telegram", "nonexistent"])
    assert result is _MISSING


# -- _load_config_or_exit --

def test_load_config_or_exit_missing(tmp_path: Path) -> None:
    import typer
    p = tmp_path / "missing.toml"
    with pytest.raises(typer.Exit) as exc_info:
        _load_config_or_exit(p, missing_code=5)
    assert exc_info.value.exit_code == 5


def test_load_config_or_exit_valid(tmp_path: Path) -> None:
    p = tmp_path / "tunapi.toml"
    p.write_text('transport = "telegram"\n')
    result = _load_config_or_exit(p, missing_code=1)
    assert result["transport"] == "telegram"


def test_load_config_or_exit_invalid(tmp_path: Path) -> None:
    import typer
    p = tmp_path / "tunapi.toml"
    p.write_text("invalid toml {{{")
    with pytest.raises(typer.Exit):
        _load_config_or_exit(p, missing_code=1)


# -- config_path_cmd --

def test_config_path_cmd(capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "tunapi.cli.config._resolve_config_path_override",
        lambda _: Path.home() / ".tunapi" / "tunapi.toml",
    )
    config_path_cmd(config_path=None)
    captured = capsys.readouterr()
    assert "tunapi.toml" in captured.out


# -- config_list --

def test_config_list(tmp_path: Path, capsys, monkeypatch) -> None:
    p = tmp_path / "tunapi.toml"
    p.write_text('transport = "telegram"\nwatch_config = true\n')
    monkeypatch.setattr(
        "tunapi.cli.config._resolve_config_path_override", lambda _: p
    )
    config_list(config_path=None)
    captured = capsys.readouterr()
    assert "transport" in captured.out
    assert "watch_config" in captured.out


# -- config_get --

def test_config_get(tmp_path: Path, capsys, monkeypatch) -> None:
    p = tmp_path / "tunapi.toml"
    p.write_text('transport = "telegram"\n')
    monkeypatch.setattr(
        "tunapi.cli.config._resolve_config_path_override", lambda _: p
    )
    config_get(key="transport", config_path=None)
    captured = capsys.readouterr()
    assert "telegram" in captured.out


def test_config_get_missing_key(tmp_path: Path, monkeypatch) -> None:
    import typer
    p = tmp_path / "tunapi.toml"
    p.write_text('transport = "telegram"\n')
    monkeypatch.setattr(
        "tunapi.cli.config._resolve_config_path_override", lambda _: p
    )
    with pytest.raises(typer.Exit) as exc_info:
        config_get(key="missing_key", config_path=None)
    assert exc_info.value.exit_code == 1


def test_config_get_table_key(tmp_path: Path, capsys, monkeypatch) -> None:
    import typer
    p = tmp_path / "tunapi.toml"
    p.write_text('[transports]\n[transports.telegram]\nbot_token = "t"\nchat_id = 1\n')
    monkeypatch.setattr(
        "tunapi.cli.config._resolve_config_path_override", lambda _: p
    )
    with pytest.raises(typer.Exit) as exc_info:
        config_get(key="transports", config_path=None)
    assert exc_info.value.exit_code == 2
    captured = capsys.readouterr()
    assert "table" in captured.err


# ---------------------------------------------------------------------------
# 3. slack/render.py
# ---------------------------------------------------------------------------
from tunapi.slack.render import (
    escape_slack,
    markdown_to_mrkdwn,
    prepare_slack,
    prepare_slack_multi,
    split_mrkdwn_body,
    trim_body,
)
from tunapi.markdown import MarkdownParts


# -- escape_slack --

def test_escape_slack() -> None:
    assert escape_slack("a & b") == "a &amp; b"
    assert escape_slack("<tag>") == "&lt;tag&gt;"
    assert escape_slack("a & <b> & c") == "a &amp; &lt;b&gt; &amp; c"


def test_escape_slack_no_change() -> None:
    assert escape_slack("plain text") == "plain text"


# -- markdown_to_mrkdwn --

def test_markdown_to_mrkdwn_empty() -> None:
    assert markdown_to_mrkdwn("") == ""


def test_markdown_to_mrkdwn_link() -> None:
    result = markdown_to_mrkdwn("[click](http://example.com)")
    assert result == "<http://example.com|click>"


def test_markdown_to_mrkdwn_bold() -> None:
    result = markdown_to_mrkdwn("**bold text**")
    assert result == "*bold text*"


def test_markdown_to_mrkdwn_escape_and_link() -> None:
    result = markdown_to_mrkdwn("[A & B](http://example.com?a=1&b=2)")
    assert "&amp;" in result
    assert "<http://example.com?a=1&amp;b=2|A &amp; B>" == result


def test_markdown_to_mrkdwn_mixed() -> None:
    text = "Hello **world** and [link](http://example.com)"
    result = markdown_to_mrkdwn(text)
    assert "*world*" in result
    assert "<http://example.com|link>" in result


# -- trim_body --

def test_trim_body_none() -> None:
    assert trim_body(None) is None


def test_trim_body_empty() -> None:
    assert trim_body("") is None
    assert trim_body("   ") is None


def test_trim_body_short() -> None:
    assert trim_body("hello", max_chars=100) == "hello"


def test_trim_body_long() -> None:
    long = "x" * 100
    result = trim_body(long, max_chars=50)
    assert len(result) == 50
    assert result.endswith("…")


# -- split_mrkdwn_body --

def test_split_mrkdwn_body_short() -> None:
    body = "short text"
    result = split_mrkdwn_body(body, 100)
    assert result == ["short text"]


def test_split_mrkdwn_body_long() -> None:
    para1 = "a" * 50
    para2 = "b" * 50
    body = f"{para1}\n\n{para2}"
    result = split_mrkdwn_body(body, 60)
    assert len(result) == 2
    assert result[0].strip() == para1
    assert result[1].strip() == para2


def test_split_mrkdwn_body_fence_handling() -> None:
    body = "text\n\n```\ncode block\n```\n\nmore text"
    result = split_mrkdwn_body(body, 20)
    assert len(result) >= 2


def test_split_mrkdwn_body_single_large_paragraph() -> None:
    body = "x" * 200
    result = split_mrkdwn_body(body, 100)
    # Should still return at least something
    assert len(result) >= 1


# -- prepare_slack --

def test_prepare_slack_basic() -> None:
    parts = MarkdownParts(header="**Header**", body="some body", footer="footer")
    result = prepare_slack(parts)
    assert "*Header*" in result
    assert "some body" in result
    assert "footer" in result


def test_prepare_slack_no_body() -> None:
    parts = MarkdownParts(header="Header")
    result = prepare_slack(parts)
    assert "Header" in result


# -- prepare_slack_multi --

def test_prepare_slack_multi_short() -> None:
    parts = MarkdownParts(header="H", body="short", footer="F")
    result = prepare_slack_multi(parts)
    assert len(result) == 1


def test_prepare_slack_multi_no_body() -> None:
    parts = MarkdownParts(header="H", body="", footer="F")
    result = prepare_slack_multi(parts)
    assert len(result) == 1


def test_prepare_slack_multi_long_body() -> None:
    para1 = "a" * 100
    para2 = "b" * 100
    parts = MarkdownParts(header="H", body=f"{para1}\n\n{para2}", footer="F")
    result = prepare_slack_multi(parts, max_body_chars=120)
    assert len(result) == 2
    # First chunk has header
    assert "H" in result[0]
    # Last chunk has footer
    assert "F" in result[-1]
    # Middle chunks should have "continued" marker
    if len(result) > 1:
        assert "continued" in result[1]


# ---------------------------------------------------------------------------
# 4. telegram/onboarding.py
# ---------------------------------------------------------------------------
from tunapi.telegram.onboarding import (
    ChatInfo,
    OnboardingCancelled,
    OnboardingState,
    check_setup,
    config_issue,
    display_path,
    mask_token,
    require_value,
)


# -- ChatInfo --

def test_chat_info_is_group() -> None:
    c = ChatInfo(chat_id=1, username=None, title="Test", first_name=None, last_name=None, chat_type="supergroup")
    assert c.is_group is True


def test_chat_info_is_not_group() -> None:
    c = ChatInfo(chat_id=1, username="bob", title=None, first_name=None, last_name=None, chat_type="private")
    assert c.is_group is False


def test_chat_info_display_group() -> None:
    c = ChatInfo(chat_id=1, username=None, title="My Group", first_name=None, last_name=None, chat_type="group")
    assert c.display == 'group "My Group"'


def test_chat_info_display_group_no_title() -> None:
    c = ChatInfo(chat_id=1, username=None, title=None, first_name=None, last_name=None, chat_type="group")
    assert c.display == "group chat"


def test_chat_info_display_channel() -> None:
    c = ChatInfo(chat_id=1, username=None, title="Chan", first_name=None, last_name=None, chat_type="channel")
    assert c.display == 'channel "Chan"'


def test_chat_info_display_channel_no_title() -> None:
    c = ChatInfo(chat_id=1, username=None, title=None, first_name=None, last_name=None, chat_type="channel")
    assert c.display == "channel"


def test_chat_info_display_private_username() -> None:
    c = ChatInfo(chat_id=1, username="alice", title=None, first_name=None, last_name=None, chat_type="private")
    assert c.display == "@alice"


def test_chat_info_display_private_name() -> None:
    c = ChatInfo(chat_id=1, username=None, title=None, first_name="John", last_name="Doe", chat_type="private")
    assert c.display == "John Doe"


def test_chat_info_display_private_no_info() -> None:
    c = ChatInfo(chat_id=1, username=None, title=None, first_name=None, last_name=None, chat_type="private")
    assert c.display == "private chat"


def test_chat_info_kind_private() -> None:
    c = ChatInfo(chat_id=1, username=None, title=None, first_name=None, last_name=None, chat_type="private")
    assert c.kind == "private chat"


def test_chat_info_kind_none() -> None:
    c = ChatInfo(chat_id=1, username=None, title=None, first_name=None, last_name=None, chat_type=None)
    assert c.kind == "private chat"


def test_chat_info_kind_group_with_title() -> None:
    c = ChatInfo(chat_id=1, username=None, title="Dev", first_name=None, last_name=None, chat_type="supergroup")
    assert c.kind == 'supergroup "Dev"'


def test_chat_info_kind_group_no_title() -> None:
    c = ChatInfo(chat_id=1, username=None, title=None, first_name=None, last_name=None, chat_type="group")
    assert c.kind == "group"


def test_chat_info_kind_channel_with_title() -> None:
    c = ChatInfo(chat_id=1, username=None, title="News", first_name=None, last_name=None, chat_type="channel")
    assert c.kind == 'channel "News"'


def test_chat_info_kind_channel_no_title() -> None:
    c = ChatInfo(chat_id=1, username=None, title=None, first_name=None, last_name=None, chat_type="channel")
    assert c.kind == "channel"


def test_chat_info_kind_unknown() -> None:
    c = ChatInfo(chat_id=1, username=None, title=None, first_name=None, last_name=None, chat_type="other_type")
    assert c.kind == "other_type"


# -- OnboardingState --

def test_onboarding_state_is_stateful() -> None:
    s = OnboardingState(config_path=Path("/tmp"), force=False, session_mode="chat")
    assert s.is_stateful is True

    s2 = OnboardingState(config_path=Path("/tmp"), force=False, topics_enabled=True)
    assert s2.is_stateful is True

    s3 = OnboardingState(config_path=Path("/tmp"), force=False)
    assert s3.is_stateful is False


def test_onboarding_state_bot_ref() -> None:
    s = OnboardingState(config_path=Path("/tmp"), force=False, bot_username="mybot")
    assert s.bot_ref == "@mybot"

    s2 = OnboardingState(config_path=Path("/tmp"), force=False, bot_name="TunaBot")
    assert s2.bot_ref == "TunaBot"

    s3 = OnboardingState(config_path=Path("/tmp"), force=False)
    assert s3.bot_ref == "your bot"


# -- mask_token --

def test_mask_token_short() -> None:
    assert mask_token("abc") == "***"


def test_mask_token_exact_12() -> None:
    assert mask_token("123456789012") == "************"


def test_mask_token_long() -> None:
    token = "1234567890123456789012345"
    result = mask_token(token)
    assert result.startswith("123456789")
    assert "..." in result
    assert result == "123456789...12345"


def test_mask_token_strips_whitespace() -> None:
    result = mask_token("  abc  ")
    assert result == "***"


# -- require_value --

def test_require_value_ok() -> None:
    assert require_value(42) == 42
    assert require_value("hello") == "hello"


def test_require_value_none() -> None:
    with pytest.raises(OnboardingCancelled):
        require_value(None)


# -- display_path --

def test_display_path_home() -> None:
    home = Path.home()
    p = home / "foo" / "bar"
    assert display_path(p) == "~/foo/bar"


def test_display_path_non_home() -> None:
    p = Path("/etc/tunapi/config.toml")
    result = display_path(p)
    assert result == "/etc/tunapi/config.toml"


# -- config_issue --

def test_config_issue() -> None:
    p = Path("/etc/tunapi.toml")
    issue = config_issue(p, title="test issue")
    assert issue.title == "test issue"
    assert len(issue.lines) == 1
    assert "/etc/tunapi.toml" in issue.lines[0]


# -- check_setup --

def test_check_setup_missing_cli(monkeypatch) -> None:
    backend = MagicMock()
    backend.cli_cmd = "nonexistent_binary_xyz"
    backend.id = "test"
    backend.install_cmd = "pip install test"

    monkeypatch.setattr(
        "tunapi.telegram.onboarding.load_settings",
        MagicMock(side_effect=ConfigError("no config")),
    )
    result = check_setup(backend, transport_override="telegram")
    assert len(result.issues) > 0


def test_check_setup_non_telegram(monkeypatch) -> None:
    backend = MagicMock()
    backend.cli_cmd = "nonexistent_binary_xyz"
    backend.id = "test"
    backend.install_cmd = None

    monkeypatch.setattr(
        "tunapi.telegram.onboarding.load_settings",
        MagicMock(side_effect=ConfigError("no config")),
    )
    result = check_setup(backend, transport_override="mattermost")
    # Should show create config issue since transport != telegram
    assert any("create" in i.title for i in result.issues)


# ---------------------------------------------------------------------------
# 5. telegram/loop_state.py helpers
# ---------------------------------------------------------------------------
from tunapi.telegram.loop_state import (
    allowed_chat_ids,
    chat_session_key,
    classify_message,
    diff_keys,
)


# -- diff_keys --

def test_diff_keys_no_diff() -> None:
    assert diff_keys({"a": 1, "b": 2}, {"a": 1, "b": 2}) == []


def test_diff_keys_changed() -> None:
    result = diff_keys({"a": 1, "b": 2}, {"a": 1, "b": 3})
    assert result == ["b"]


def test_diff_keys_added_removed() -> None:
    result = diff_keys({"a": 1}, {"b": 2})
    assert sorted(result) == ["a", "b"]


def test_diff_keys_empty() -> None:
    assert diff_keys({}, {}) == []


# -- chat_session_key --

def test_chat_session_key_no_store() -> None:
    msg = MagicMock()
    msg.thread_id = None
    msg.chat_type = "private"
    msg.chat_id = 100
    assert chat_session_key(msg, store=None) is None


def test_chat_session_key_with_thread() -> None:
    msg = MagicMock()
    msg.thread_id = 42
    store = MagicMock()
    assert chat_session_key(msg, store=store) is None


def test_chat_session_key_private() -> None:
    msg = MagicMock()
    msg.thread_id = None
    msg.chat_type = "private"
    msg.chat_id = 100
    store = MagicMock()
    assert chat_session_key(msg, store=store) == (100, None)


def test_chat_session_key_group() -> None:
    msg = MagicMock()
    msg.thread_id = None
    msg.chat_type = "group"
    msg.chat_id = 100
    msg.sender_id = 42
    store = MagicMock()
    assert chat_session_key(msg, store=store) == (100, 42)


def test_chat_session_key_group_no_sender() -> None:
    msg = MagicMock()
    msg.thread_id = None
    msg.chat_type = "group"
    msg.chat_id = 100
    msg.sender_id = None
    store = MagicMock()
    assert chat_session_key(msg, store=store) is None


# -- classify_message --

def test_classify_message_normal() -> None:
    msg = MagicMock()
    msg.text = "hello world"
    msg.document = None
    msg.voice = None
    msg.media_group_id = None
    msg.raw = {}
    result = classify_message(msg, files_enabled=True)
    assert result.text == "hello world"
    assert result.command_id is None
    assert result.is_cancel is False
    assert result.is_forward_candidate is False
    assert result.is_media_group_document is False


def test_classify_message_cancel() -> None:
    msg = MagicMock()
    msg.text = "/cancel"
    msg.document = None
    msg.voice = None
    msg.media_group_id = None
    msg.raw = {}
    result = classify_message(msg, files_enabled=False)
    assert result.is_cancel is True


def test_classify_message_media_group_document() -> None:
    msg = MagicMock()
    msg.text = ""
    msg.document = MagicMock()
    msg.voice = None
    msg.media_group_id = "group123"
    msg.raw = {}
    result = classify_message(msg, files_enabled=True)
    assert result.is_media_group_document is True


def test_classify_message_media_group_doc_files_disabled() -> None:
    msg = MagicMock()
    msg.text = ""
    msg.document = MagicMock()
    msg.voice = None
    msg.media_group_id = "group123"
    msg.raw = {}
    result = classify_message(msg, files_enabled=False)
    assert result.is_media_group_document is False


# -- allowed_chat_ids --

def test_allowed_chat_ids() -> None:
    cfg = MagicMock()
    cfg.chat_ids = [1, 2]
    cfg.chat_id = 3
    cfg.runtime.project_chat_ids.return_value = [4]
    cfg.allowed_user_ids = [5]
    result = allowed_chat_ids(cfg)
    assert result == {1, 2, 3, 4, 5}


def test_allowed_chat_ids_none_chat_ids() -> None:
    cfg = MagicMock()
    cfg.chat_ids = None
    cfg.chat_id = 10
    cfg.runtime.project_chat_ids.return_value = []
    cfg.allowed_user_ids = []
    result = allowed_chat_ids(cfg)
    assert result == {10}


# ---------------------------------------------------------------------------
# 6. discord/commands/registration.py
# ---------------------------------------------------------------------------
from tunapi.discord.commands.registration import (
    _format_plugin_starter_message,
    discover_command_ids,
)


# -- discover_command_ids --

def test_discover_command_ids(monkeypatch) -> None:
    monkeypatch.setattr(
        "tunapi.discord.commands.registration.list_command_ids",
        lambda allowlist=None: ["Help", "Model", "Status"],
    )
    result = discover_command_ids(allowlist=None)
    assert result == {"help", "model", "status"}


def test_discover_command_ids_with_allowlist(monkeypatch) -> None:
    monkeypatch.setattr(
        "tunapi.discord.commands.registration.list_command_ids",
        lambda allowlist=None: ["Help"] if allowlist == {"help"} else [],
    )
    result = discover_command_ids(allowlist={"help"})
    assert result == {"help"}


# -- _format_plugin_starter_message --

def test_format_plugin_starter_message_short() -> None:
    result = _format_plugin_starter_message("help", "")
    assert result == "/help"


def test_format_plugin_starter_message_with_args() -> None:
    result = _format_plugin_starter_message("model", "claude opus")
    assert result == "/model claude opus"


def test_format_plugin_starter_message_truncated() -> None:
    long_args = "x" * 2000
    result = _format_plugin_starter_message("cmd", long_args, max_chars=50)
    assert len(result) == 50
    assert result.endswith("…")


def test_format_plugin_starter_message_exact_limit() -> None:
    result = _format_plugin_starter_message("ab", "cd", max_chars=6)
    assert result == "/ab cd"
    assert len(result) == 6
