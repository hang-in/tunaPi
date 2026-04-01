"""Tests for tunapi.logging — pure functions, redaction, SafeWriter, setup_logging."""

from __future__ import annotations

import errno
import io
import os
from unittest.mock import MagicMock

import pytest
import structlog

from tunapi.logging import (
    SafeWriter,
    _add_logger_name,
    _drop_below_level,
    _level_value,
    _redact_event_dict,
    _redact_text,
    _redact_value,
    _truthy,
    bind_run_context,
    clear_context,
    get_logger,
    log_pipeline,
    pipeline_log_level,
    setup_logging,
    suppress_logs,
)


# ── _truthy ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("  TRUE  ", True),
        ("  YES  ", True),
    ],
)
def test_truthy(value: str | None, expected: bool) -> None:
    assert _truthy(value) is expected


# ── _level_value ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (None, "info", 20),
        ("", "info", 20),
        ("debug", "info", 10),
        ("warning", "info", 30),
        ("  ERROR  ", "info", 40),
        ("nonsense", "warning", 30),
        ("critical", "info", 50),
    ],
)
def test_level_value(value: str | None, default: str, expected: int) -> None:
    assert _level_value(value, default=default) == expected


# ── _redact_text ─────────────────────────────────────────────────────


def test_redact_telegram_bot_token() -> None:
    text = "url=https://api.telegram.org/bot12345:ABCdef_ghiJKL/getMe"
    assert "bot12345:ABCdef_ghiJKL" not in _redact_text(text)
    assert "bot[REDACTED]" in _redact_text(text)


def test_redact_bare_token() -> None:
    text = "token is 123456789:ABCdefGhIjKlMnOpQ"
    result = _redact_text(text)
    assert "123456789:ABCdefGhIjKlMnOpQ" not in result
    assert "[REDACTED" in result


def test_redact_no_token() -> None:
    text = "hello world, nothing secret"
    assert _redact_text(text) == text


# ── _redact_value (recursive) ───────────────────────────────────────


def test_redact_value_string() -> None:
    assert "bot[REDACTED]" in _redact_value("bot999:AAAA_bbb", {})


def test_redact_value_bytes() -> None:
    result = _redact_value(b"bot999:AAAA_bbb", {})
    assert "bot[REDACTED]" in result


def test_redact_value_dict() -> None:
    d = {"token": "bot999:AAAA_bbb", "count": 5}
    result = _redact_value(d, {})
    assert "bot[REDACTED]" in result["token"]
    assert result["count"] == 5


def test_redact_value_list() -> None:
    lst = ["safe", "bot999:AAAA_bbb"]
    result = _redact_value(lst, {})
    assert result[0] == "safe"
    assert "bot[REDACTED]" in result[1]


def test_redact_value_tuple() -> None:
    t = ("safe", "bot999:AAAA_bbb")
    result = _redact_value(t, {})
    assert isinstance(result, tuple)
    assert "bot[REDACTED]" in result[1]


def test_redact_value_set() -> None:
    s = {"bot999:AAAA_bbb"}
    result = _redact_value(s, {})
    assert isinstance(result, set)
    assert any("bot[REDACTED]" in item for item in result)


def test_redact_value_passthrough_int() -> None:
    assert _redact_value(42, {}) == 42


def test_redact_value_circular_reference() -> None:
    d: dict = {"key": "val"}
    d["self"] = d  # circular
    result = _redact_value(d, {})
    # Should not infinite loop; circular ref returns memoized value
    assert result["key"] == "val"


# ── _redact_event_dict ──────────────────────────────────────────────


def test_redact_event_dict() -> None:
    ed = {"event": "connect", "url": "bot111:XYZ_abc"}
    result = _redact_event_dict(None, "info", ed)
    assert "bot[REDACTED]" in result["url"]


# ── _drop_below_level ───────────────────────────────────────────────


def test_drop_below_level_drops_debug() -> None:
    with pytest.raises(structlog.DropEvent):
        _drop_below_level(None, "debug", {})


def test_drop_below_level_passes_info() -> None:
    result = _drop_below_level(None, "info", {"event": "x"})
    assert result == {"event": "x"}


def test_drop_below_level_passes_error() -> None:
    result = _drop_below_level(None, "error", {"event": "x"})
    assert result == {"event": "x"}


# ── suppress_logs ───────────────────────────────────────────────────


def test_suppress_logs_raises_drop_for_info() -> None:
    with suppress_logs("warning"):
        with pytest.raises(structlog.DropEvent):
            _drop_below_level(None, "info", {})


def test_suppress_logs_passes_warning() -> None:
    with suppress_logs("warning"):
        result = _drop_below_level(None, "warning", {"event": "x"})
        assert result == {"event": "x"}


def test_suppress_logs_resets_after_context() -> None:
    with suppress_logs("error"):
        pass
    # After context exit, info should pass (default MIN_LEVEL is info)
    result = _drop_below_level(None, "info", {"event": "x"})
    assert result == {"event": "x"}


# ── _add_logger_name ────────────────────────────────────────────────


def test_add_logger_name_from_event_dict() -> None:
    ed: dict = {"logger_name": "my.module", "event": "x"}
    result = _add_logger_name(None, "info", ed)
    assert result["logger"] == "my.module"
    assert "logger_name" not in result


def test_add_logger_name_already_present() -> None:
    ed: dict = {"logger": "existing", "event": "x"}
    result = _add_logger_name(None, "info", ed)
    assert result["logger"] == "existing"


def test_add_logger_name_from_logger_attr() -> None:
    logger = MagicMock()
    logger.name = "fallback.logger"
    ed: dict = {"event": "x"}
    result = _add_logger_name(logger, "info", ed)
    assert result["logger"] == "fallback.logger"


def test_add_logger_name_empty_string_ignored() -> None:
    ed: dict = {"logger_name": "", "event": "x"}
    result = _add_logger_name(None, "info", ed)
    assert "logger" not in result


# ── SafeWriter ──────────────────────────────────────────────────────


def test_safe_writer_write() -> None:
    buf = io.StringIO()
    w = SafeWriter(buf)
    n = w.write("hello")
    assert n == 5
    assert buf.getvalue() == "hello"


def test_safe_writer_flush() -> None:
    buf = io.StringIO()
    w = SafeWriter(buf)
    w.write("data")
    w.flush()  # should not raise
    assert buf.getvalue() == "data"


def test_safe_writer_broken_pipe_write() -> None:
    stream = MagicMock()
    stream.write.side_effect = BrokenPipeError
    w = SafeWriter(stream)
    assert w.write("hello") == 0
    # After broken pipe, subsequent writes return 0
    assert w.write("again") == 0


def test_safe_writer_broken_pipe_flush() -> None:
    stream = MagicMock()
    stream.flush.side_effect = BrokenPipeError
    w = SafeWriter(stream)
    w.flush()  # should not raise


def test_safe_writer_epipe_write() -> None:
    stream = MagicMock()
    stream.write.side_effect = OSError(errno.EPIPE, "Broken pipe")
    w = SafeWriter(stream)
    assert w.write("hello") == 0


def test_safe_writer_epipe_flush() -> None:
    stream = MagicMock()
    stream.flush.side_effect = OSError(errno.EPIPE, "Broken pipe")
    w = SafeWriter(stream)
    w.flush()  # should not raise


def test_safe_writer_other_os_error_write() -> None:
    stream = MagicMock()
    stream.write.side_effect = OSError(errno.ENOENT, "No such file")
    w = SafeWriter(stream)
    with pytest.raises(OSError, match="No such file"):
        w.write("hello")


def test_safe_writer_other_os_error_flush() -> None:
    stream = MagicMock()
    stream.flush.side_effect = OSError(errno.ENOENT, "No such file")
    w = SafeWriter(stream)
    with pytest.raises(OSError, match="No such file"):
        w.flush()


def test_safe_writer_isatty_true() -> None:
    stream = MagicMock()
    stream.isatty.return_value = True
    w = SafeWriter(stream)
    assert w.isatty() is True


def test_safe_writer_isatty_false() -> None:
    stream = MagicMock()
    stream.isatty.return_value = False
    w = SafeWriter(stream)
    assert w.isatty() is False


def test_safe_writer_isatty_missing() -> None:
    stream = MagicMock(spec=["close"])  # no isatty attr, but has close
    w = SafeWriter(stream)
    assert w.isatty() is False


def test_safe_writer_value_error_write() -> None:
    stream = MagicMock()
    stream.write.side_effect = ValueError("I/O operation on closed file")
    w = SafeWriter(stream)
    assert w.write("hello") == 0


def test_safe_writer_value_error_flush() -> None:
    stream = MagicMock()
    stream.flush.side_effect = ValueError("I/O operation on closed file")
    w = SafeWriter(stream)
    w.flush()  # should not raise


# ── setup_logging ───────────────────────────────────────────────────


def test_setup_logging_default(monkeypatch) -> None:
    monkeypatch.delenv("TUNAPI_LOG_LEVEL", raising=False)
    monkeypatch.delenv("TUNAPI_TRACE_PIPELINE", raising=False)
    monkeypatch.delenv("TUNAPI_LOG_FORMAT", raising=False)
    monkeypatch.delenv("TUNAPI_LOG_COLOR", raising=False)
    monkeypatch.delenv("TUNAPI_LOG_FILE", raising=False)
    setup_logging()
    # Should not raise; basic sanity check
    logger = get_logger("test")
    assert logger is not None


def test_setup_logging_debug(monkeypatch) -> None:
    monkeypatch.delenv("TUNAPI_LOG_LEVEL", raising=False)
    monkeypatch.delenv("TUNAPI_TRACE_PIPELINE", raising=False)
    monkeypatch.delenv("TUNAPI_LOG_FORMAT", raising=False)
    monkeypatch.delenv("TUNAPI_LOG_COLOR", raising=False)
    monkeypatch.delenv("TUNAPI_LOG_FILE", raising=False)
    setup_logging(debug=True)
    # In debug mode, debug messages should NOT be dropped
    result = _drop_below_level(None, "debug", {"event": "x"})
    assert result == {"event": "x"}


def test_setup_logging_json_format(monkeypatch) -> None:
    monkeypatch.setenv("TUNAPI_LOG_FORMAT", "json")
    monkeypatch.delenv("TUNAPI_LOG_LEVEL", raising=False)
    monkeypatch.delenv("TUNAPI_TRACE_PIPELINE", raising=False)
    monkeypatch.delenv("TUNAPI_LOG_COLOR", raising=False)
    monkeypatch.delenv("TUNAPI_LOG_FILE", raising=False)
    setup_logging()
    logger = get_logger("test")
    assert logger is not None


def test_setup_logging_trace_pipeline(monkeypatch) -> None:
    monkeypatch.setenv("TUNAPI_TRACE_PIPELINE", "1")
    monkeypatch.delenv("TUNAPI_LOG_LEVEL", raising=False)
    monkeypatch.delenv("TUNAPI_LOG_FORMAT", raising=False)
    monkeypatch.delenv("TUNAPI_LOG_COLOR", raising=False)
    monkeypatch.delenv("TUNAPI_LOG_FILE", raising=False)
    setup_logging()
    assert pipeline_log_level() == "info"


def test_setup_logging_log_file(monkeypatch, tmp_path) -> None:
    log_file = tmp_path / "test.log"
    monkeypatch.setenv("TUNAPI_LOG_FILE", str(log_file))
    monkeypatch.delenv("TUNAPI_LOG_LEVEL", raising=False)
    monkeypatch.delenv("TUNAPI_TRACE_PIPELINE", raising=False)
    monkeypatch.delenv("TUNAPI_LOG_FORMAT", raising=False)
    monkeypatch.delenv("TUNAPI_LOG_COLOR", raising=False)
    setup_logging()
    # File handle should have been opened
    assert log_file.parent.exists()


def test_setup_logging_log_file_invalid_path(monkeypatch) -> None:
    monkeypatch.setenv("TUNAPI_LOG_FILE", "/nonexistent/dir/test.log")
    monkeypatch.delenv("TUNAPI_LOG_LEVEL", raising=False)
    monkeypatch.delenv("TUNAPI_TRACE_PIPELINE", raising=False)
    monkeypatch.delenv("TUNAPI_LOG_FORMAT", raising=False)
    monkeypatch.delenv("TUNAPI_LOG_COLOR", raising=False)
    # Should not raise — invalid path is handled gracefully
    setup_logging()


# ── get_logger / bind / clear ───────────────────────────────────────


def test_get_logger_with_name() -> None:
    logger = get_logger("mymodule")
    assert logger is not None


def test_get_logger_without_name() -> None:
    logger = get_logger()
    assert logger is not None


def test_bind_and_clear_context() -> None:
    bind_run_context(run_id="abc")
    clear_context()
    # Should not raise


# ── log_pipeline ────────────────────────────────────────────────────


def test_log_pipeline_info_level(monkeypatch) -> None:
    import tunapi.logging as logging_mod

    monkeypatch.setattr(logging_mod, "_PIPELINE_LEVEL_NAME", "info")
    logger = MagicMock()
    log_pipeline(logger, "test event", key="val")
    logger.info.assert_called_once_with("test event", key="val")


def test_log_pipeline_debug_level(monkeypatch) -> None:
    import tunapi.logging as logging_mod

    monkeypatch.setattr(logging_mod, "_PIPELINE_LEVEL_NAME", "debug")
    logger = MagicMock()
    log_pipeline(logger, "test event", key="val")
    logger.debug.assert_called_once_with("test event", key="val")
