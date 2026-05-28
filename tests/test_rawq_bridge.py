"""Tests for tunadish/rawq_bridge.py — rawq CLI bridge."""
# ruff: noqa: E402

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tunapi.tunadish.rawq_bridge import (
    _DEFAULT_EXCLUDE,
    _find_rawq,
    format_context_block,
    format_map_block,
    is_available,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# format_context_block
# ---------------------------------------------------------------------------


class TestFormatContextBlock:
    def test_empty_results(self):
        assert format_context_block({"results": []}) == ""
        assert format_context_block({}) == ""

    def test_single_result(self):
        data = {
            "results": [
                {
                    "file": "src/main.py",
                    "lines": [10, 20],
                    "language": "python",
                    "scope": "def hello",
                    "confidence": 0.85,
                    "content": "def hello():\n    print('hi')",
                }
            ]
        }
        result = format_context_block(data)
        assert "<relevant_code>" in result
        assert "</relevant_code>" in result
        assert "src/main.py:10-20" in result
        assert "(def hello)" in result
        assert "[confidence: 0.85]" in result
        assert "```python" in result
        assert "def hello():" in result

    def test_multiple_results(self):
        data = {
            "results": [
                {
                    "file": "a.py",
                    "lines": [1, 5],
                    "language": "python",
                    "scope": "",
                    "confidence": 0.9,
                    "content": "code1",
                },
                {
                    "file": "b.py",
                    "lines": [10, 15],
                    "language": "python",
                    "scope": "",
                    "confidence": 0.7,
                    "content": "code2",
                },
            ]
        }
        result = format_context_block(data)
        assert "a.py" in result
        assert "b.py" in result

    def test_missing_optional_fields(self):
        data = {
            "results": [
                {
                    "file": "test.py",
                    "content": "pass",
                }
            ]
        }
        result = format_context_block(data)
        assert "test.py" in result
        assert "[confidence: 0.00]" in result

    def test_no_lines_no_range(self):
        data = {
            "results": [
                {
                    "file": "test.py",
                    "lines": [],
                    "language": "",
                    "scope": "",
                    "confidence": 0.5,
                    "content": "x = 1",
                }
            ]
        }
        result = format_context_block(data)
        # No :start-end in header
        assert "test.py  [confidence:" in result

    def test_content_trailing_whitespace_stripped(self):
        data = {
            "results": [
                {
                    "file": "a.py",
                    "lines": [],
                    "language": "python",
                    "scope": "",
                    "confidence": 0.5,
                    "content": "code   \n\n",
                }
            ]
        }
        result = format_context_block(data)
        # content should be rstripped
        assert "code" in result
        assert "code   \n" not in result


# ---------------------------------------------------------------------------
# format_map_block
# ---------------------------------------------------------------------------


class TestFormatMapBlock:
    def test_empty_files(self):
        assert format_map_block({"files": []}) == ""
        assert format_map_block({}) == ""

    def test_files_without_symbols(self):
        data = {
            "files": [
                {"path": "src/main.py", "symbols": []},
                {"path": "src/utils.py", "symbols": []},
            ]
        }
        result = format_map_block(data)
        assert "<project_structure>" in result
        assert "</project_structure>" in result
        assert "  src/main.py" in result
        assert "  src/utils.py" in result

    def test_files_with_symbols(self):
        data = {
            "files": [
                {
                    "path": "src/app.py",
                    "symbols": [
                        {"name": "App"},
                        {"name": "run"},
                        {"name": "init"},
                    ],
                }
            ]
        }
        result = format_map_block(data)
        assert "src/app.py (App, run, init)" in result

    def test_symbols_capped_at_8(self):
        data = {
            "files": [
                {
                    "path": "big.py",
                    "symbols": [{"name": f"sym{i}"} for i in range(12)],
                }
            ]
        }
        result = format_map_block(data)
        assert "..." in result
        # First 8 should be present
        assert "sym0" in result
        assert "sym7" in result

    def test_empty_symbol_names_filtered(self):
        data = {
            "files": [
                {
                    "path": "a.py",
                    "symbols": [{"name": ""}, {"name": "real"}, {}],
                }
            ]
        }
        result = format_map_block(data)
        assert "(real)" in result


# ---------------------------------------------------------------------------
# _find_rawq / is_available / _get_rawq
# ---------------------------------------------------------------------------


class TestFindRawq:
    def test_env_bin_takes_priority(self, tmp_path):
        fake_bin = tmp_path / "rawq.exe"
        fake_bin.write_text("fake")
        with patch.dict("os.environ", {"RAWQ_BIN": str(fake_bin)}):
            result = _find_rawq()
            assert result == str(fake_bin)

    def test_env_bin_nonexistent_skipped(self):
        with patch.dict("os.environ", {"RAWQ_BIN": "/nonexistent/rawq"}):
            # Should not return the env path if file doesn't exist
            result = _find_rawq()
            # Result depends on PATH/vendor, but should NOT be the env path
            assert result != "/nonexistent/rawq"

    def test_which_fallback(self, tmp_path):
        with (
            patch.dict("os.environ", {"RAWQ_BIN": ""}),
            patch("shutil.which", return_value="/usr/bin/rawq"),
        ):
            result = _find_rawq()
            assert result == "/usr/bin/rawq"


class TestGetRawqCaching:
    def test_caching_behavior(self):
        """_get_rawq should cache the result after first call."""
        import tunapi.tunadish.rawq_bridge as rb

        # Save original state
        orig_path = rb._rawq_path
        orig_checked = rb._rawq_checked

        try:
            rb._rawq_checked = False
            rb._rawq_path = None

            with patch.object(rb, "_find_rawq", return_value="/fake/rawq") as mock_find:
                result1 = rb._get_rawq()
                result2 = rb._get_rawq()
                assert result1 == "/fake/rawq"
                assert result2 == "/fake/rawq"
                # Should only call _find_rawq once
                mock_find.assert_called_once()
        finally:
            rb._rawq_path = orig_path
            rb._rawq_checked = orig_checked


class TestIsAvailable:
    def test_available_when_binary_found(self):
        import tunapi.tunadish.rawq_bridge as rb

        orig_path = rb._rawq_path
        orig_checked = rb._rawq_checked
        try:
            rb._rawq_checked = True
            rb._rawq_path = "/fake/rawq"
            assert is_available() is True
        finally:
            rb._rawq_path = orig_path
            rb._rawq_checked = orig_checked

    def test_unavailable_when_no_binary(self):
        import tunapi.tunadish.rawq_bridge as rb

        orig_path = rb._rawq_path
        orig_checked = rb._rawq_checked
        try:
            rb._rawq_checked = True
            rb._rawq_path = None
            assert is_available() is False
        finally:
            rb._rawq_path = orig_path
            rb._rawq_checked = orig_checked


# ---------------------------------------------------------------------------
# Default exclude patterns
# ---------------------------------------------------------------------------


class TestDefaultExclude:
    def test_common_patterns_present(self):
        assert "node_modules" in _DEFAULT_EXCLUDE
        assert ".venv" in _DEFAULT_EXCLUDE
        assert "__pycache__" in _DEFAULT_EXCLUDE
        assert ".git" in _DEFAULT_EXCLUDE

    def test_is_list(self):
        assert isinstance(_DEFAULT_EXCLUDE, list)
        assert len(_DEFAULT_EXCLUDE) > 0


# ---------------------------------------------------------------------------
# Async functions (search, build_index, etc.) with mocked subprocess
# ---------------------------------------------------------------------------


class TestSearchMocked:
    async def test_search_returns_none_when_unavailable(self):
        import tunapi.tunadish.rawq_bridge as rb

        orig_path = rb._rawq_path
        orig_checked = rb._rawq_checked
        try:
            rb._rawq_checked = True
            rb._rawq_path = None
            result = await rb.search("test query", "/tmp/project")
            assert result is None
        finally:
            rb._rawq_path = orig_path
            rb._rawq_checked = orig_checked

    async def test_search_returns_parsed_json(self):
        import tunapi.tunadish.rawq_bridge as rb

        orig_path = rb._rawq_path
        orig_checked = rb._rawq_checked
        try:
            rb._rawq_checked = True
            rb._rawq_path = "/fake/rawq"

            mock_result = AsyncMock()
            mock_result.returncode = 0
            mock_result.stdout = b'{"results": [{"file": "a.py"}]}'

            with patch("anyio.run_process", return_value=mock_result):
                result = await rb.search("test", "/tmp/project")
                assert result == {"results": [{"file": "a.py"}]}
        finally:
            rb._rawq_path = orig_path
            rb._rawq_checked = orig_checked

    async def test_search_returns_none_on_failure(self):
        import tunapi.tunadish.rawq_bridge as rb

        orig_path = rb._rawq_path
        orig_checked = rb._rawq_checked
        try:
            rb._rawq_checked = True
            rb._rawq_path = "/fake/rawq"

            mock_result = AsyncMock()
            mock_result.returncode = 1
            mock_result.stdout = b""

            with patch("anyio.run_process", return_value=mock_result):
                result = await rb.search("test", "/tmp/project")
                assert result is None
        finally:
            rb._rawq_path = orig_path
            rb._rawq_checked = orig_checked


class TestBuildIndexMocked:
    async def test_build_index_unavailable(self):
        import tunapi.tunadish.rawq_bridge as rb

        orig_path = rb._rawq_path
        orig_checked = rb._rawq_checked
        try:
            rb._rawq_checked = True
            rb._rawq_path = None
            result = await rb.build_index("/tmp/project")
            assert result is False
        finally:
            rb._rawq_path = orig_path
            rb._rawq_checked = orig_checked

    async def test_build_index_success(self):
        import tunapi.tunadish.rawq_bridge as rb

        orig_path = rb._rawq_path
        orig_checked = rb._rawq_checked
        try:
            rb._rawq_checked = True
            rb._rawq_path = "/fake/rawq"

            mock_result = AsyncMock()
            mock_result.returncode = 0

            with patch("anyio.run_process", return_value=mock_result):
                result = await rb.build_index("/tmp/project")
                assert result is True
        finally:
            rb._rawq_path = orig_path
            rb._rawq_checked = orig_checked


class TestGetMapMocked:
    async def test_get_map_unavailable(self):
        import tunapi.tunadish.rawq_bridge as rb

        orig_path = rb._rawq_path
        orig_checked = rb._rawq_checked
        try:
            rb._rawq_checked = True
            rb._rawq_path = None
            result = await rb.get_map("/tmp/project")
            assert result is None
        finally:
            rb._rawq_path = orig_path
            rb._rawq_checked = orig_checked

    async def test_get_map_success(self):
        import tunapi.tunadish.rawq_bridge as rb

        orig_path = rb._rawq_path
        orig_checked = rb._rawq_checked
        try:
            rb._rawq_checked = True
            rb._rawq_path = "/fake/rawq"

            mock_result = AsyncMock()
            mock_result.returncode = 0
            mock_result.stdout = b'{"files": [{"path": "a.py", "symbols": []}]}'

            with patch("anyio.run_process", return_value=mock_result):
                result = await rb.get_map("/tmp/project")
                assert result == {"files": [{"path": "a.py", "symbols": []}]}
        finally:
            rb._rawq_path = orig_path
            rb._rawq_checked = orig_checked


class TestGetVersionMocked:
    async def test_get_version_unavailable(self):
        import tunapi.tunadish.rawq_bridge as rb

        orig_path = rb._rawq_path
        orig_checked = rb._rawq_checked
        try:
            rb._rawq_checked = True
            rb._rawq_path = None
            result = await rb.get_version()
            assert result is None
        finally:
            rb._rawq_path = orig_path
            rb._rawq_checked = orig_checked

    async def test_get_version_success(self):
        import tunapi.tunadish.rawq_bridge as rb

        orig_path = rb._rawq_path
        orig_checked = rb._rawq_checked
        try:
            rb._rawq_checked = True
            rb._rawq_path = "/fake/rawq"

            mock_result = AsyncMock()
            mock_result.returncode = 0
            mock_result.stdout = b"rawq 0.2.1"

            with patch("anyio.run_process", return_value=mock_result):
                result = await rb.get_version()
                assert result == "0.2.1"
        finally:
            rb._rawq_path = orig_path
            rb._rawq_checked = orig_checked


class TestCheckIndexMocked:
    async def test_check_index_unavailable(self):
        import tunapi.tunadish.rawq_bridge as rb

        orig_path = rb._rawq_path
        orig_checked = rb._rawq_checked
        try:
            rb._rawq_checked = True
            rb._rawq_path = None
            result = await rb.check_index("/tmp/project")
            assert result is None
        finally:
            rb._rawq_path = orig_path
            rb._rawq_checked = orig_checked

    async def test_check_index_success(self):
        import tunapi.tunadish.rawq_bridge as rb

        orig_path = rb._rawq_path
        orig_checked = rb._rawq_checked
        try:
            rb._rawq_checked = True
            rb._rawq_path = "/fake/rawq"

            mock_result = AsyncMock()
            mock_result.returncode = 0
            mock_result.stdout = b'{"indexed": true, "file_count": 42}'

            with patch("anyio.run_process", return_value=mock_result):
                result = await rb.check_index("/tmp/project")
                assert result == {"indexed": True, "file_count": 42}
        finally:
            rb._rawq_path = orig_path
            rb._rawq_checked = orig_checked


class TestFormatContextBlockPush:
    def test_empty_results(self):
        assert format_context_block({"results": []}) == ""
        assert format_context_block({}) == ""

    def test_with_results(self):
        data = {
            "results": [
                {
                    "file": "main.py",
                    "lines": [10, 20],
                    "language": "python",
                    "scope": "function",
                    "confidence": 0.85,
                    "content": "def hello():\n    pass",
                },
            ],
        }
        result = format_context_block(data)
        assert "<relevant_code>" in result
        assert "main.py:10-20" in result
        assert "(function)" in result
        assert "0.85" in result
        assert "```python" in result
        assert "</relevant_code>" in result

    def test_result_without_optional_fields(self):
        data = {
            "results": [
                {
                    "file": "test.rs",
                    "lines": [],
                    "language": "",
                    "scope": "",
                    "confidence": 0,
                    "content": "fn main() {}",
                },
            ],
        }
        result = format_context_block(data)
        assert "test.rs" in result
        assert "```" in result


class TestFormatMapBlockPush:
    def test_empty(self):
        assert format_map_block({"files": []}) == ""
        assert format_map_block({}) == ""

    def test_files_with_symbols(self):
        data = {
            "files": [
                {
                    "path": "main.py",
                    "symbols": [{"name": "hello"}, {"name": "world"}],
                },
            ],
        }
        result = format_map_block(data)
        assert "<project_structure>" in result
        assert "main.py (hello, world)" in result
        assert "</project_structure>" in result

    def test_files_without_symbols(self):
        data = {
            "files": [{"path": "empty.txt", "symbols": []}],
        }
        result = format_map_block(data)
        assert "empty.txt" in result

    def test_many_symbols_truncated(self):
        syms = [{"name": f"s{i}"} for i in range(12)]
        data = {"files": [{"path": "big.py", "symbols": syms}]}
        result = format_map_block(data)
        assert "..." in result


class TestFindRawqPush:
    def test_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        bin_path = tmp_path / "rawq"
        bin_path.touch()
        monkeypatch.setenv("RAWQ_BIN", str(bin_path))
        result = _find_rawq()
        assert result == str(bin_path)

    def test_env_var_nonexistent(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RAWQ_BIN", "/nonexistent/rawq")
        with patch("shutil.which", return_value=None):
            result = _find_rawq()
        # Falls through to PATH and vendor checks
        assert result is None or isinstance(result, str)

    def test_which_found(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("RAWQ_BIN", raising=False)
        with patch("shutil.which", return_value="/usr/local/bin/rawq"):
            result = _find_rawq()
        assert result == "/usr/local/bin/rawq"


import tunapi.tunadish.rawq_bridge as rawq_mod
from tunapi.tunadish.rawq_bridge import (
    build_index,
    check_index,
    get_map,
    get_version,
)
from tunapi.tunadish.rawq_bridge import search as rawq_search


class TestRawqCheckIndexPush:
    async def test_not_available(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", None)
        result = await check_index("/some/path")
        assert result is None

    async def test_success(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        proc_result = MagicMock()
        proc_result.returncode = 0
        proc_result.stdout = b'{"indexed": true}'
        with patch("anyio.run_process", AsyncMock(return_value=proc_result)):
            result = await check_index("/project")
        assert result == {"indexed": True}

    async def test_failure(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        proc_result = MagicMock()
        proc_result.returncode = 1
        proc_result.stdout = b""
        with patch("anyio.run_process", AsyncMock(return_value=proc_result)):
            result = await check_index("/project")
        assert result is None

    async def test_exception(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        with patch("anyio.run_process", AsyncMock(side_effect=OSError("fail"))):
            result = await check_index("/project")
        assert result is None


class TestRawqBuildIndexPush:
    async def test_not_available(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", None)
        result = await build_index("/project")
        assert result is False

    async def test_success(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        proc_result = MagicMock()
        proc_result.returncode = 0
        with patch("anyio.run_process", AsyncMock(return_value=proc_result)):
            result = await build_index("/project")
        assert result is True

    async def test_failure(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        proc_result = MagicMock()
        proc_result.returncode = 1
        with patch("anyio.run_process", AsyncMock(return_value=proc_result)):
            result = await build_index("/project")
        assert result is False

    async def test_custom_exclude(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        proc_result = MagicMock()
        proc_result.returncode = 0
        with patch(
            "anyio.run_process", AsyncMock(return_value=proc_result)
        ) as mock_run:
            result = await build_index("/project", exclude=["dist", "build"])
        assert result is True
        cmd = mock_run.call_args[0][0]
        assert "-x" in cmd
        assert "dist" in cmd

    async def test_exception(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        with patch("anyio.run_process", AsyncMock(side_effect=OSError("fail"))):
            result = await build_index("/project")
        assert result is False


class TestRawqSearchPush:
    async def test_not_available(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", None)
        result = await rawq_search("query", "/project")
        assert result is None

    async def test_success(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        proc_result = MagicMock()
        proc_result.returncode = 0
        proc_result.stdout = b'{"results": []}'
        with patch("anyio.run_process", AsyncMock(return_value=proc_result)):
            result = await rawq_search("query", "/project")
        assert result == {"results": []}

    async def test_with_lang_filter(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        proc_result = MagicMock()
        proc_result.returncode = 0
        proc_result.stdout = b'{"results": []}'
        with patch(
            "anyio.run_process", AsyncMock(return_value=proc_result)
        ) as mock_run:
            await rawq_search("query", "/project", lang_filter="python")
        cmd = mock_run.call_args[0][0]
        assert "--lang" in cmd
        assert "python" in cmd

    async def test_empty_stdout(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        proc_result = MagicMock()
        proc_result.returncode = 0
        proc_result.stdout = b"   "
        with patch("anyio.run_process", AsyncMock(return_value=proc_result)):
            result = await rawq_search("query", "/project")
        assert result is None

    async def test_nonzero_exit(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        proc_result = MagicMock()
        proc_result.returncode = 1
        proc_result.stdout = b""
        with patch("anyio.run_process", AsyncMock(return_value=proc_result)):
            result = await rawq_search("query", "/project")
        assert result is None

    async def test_exception(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        with patch("anyio.run_process", AsyncMock(side_effect=OSError("fail"))):
            result = await rawq_search("query", "/project")
        assert result is None

    async def test_with_exclude(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        proc_result = MagicMock()
        proc_result.returncode = 0
        proc_result.stdout = b'{"results": []}'
        with patch(
            "anyio.run_process", AsyncMock(return_value=proc_result)
        ) as mock_run:
            await rawq_search("query", "/project", exclude=["node_modules"])
        cmd = mock_run.call_args[0][0]
        assert "--exclude" in cmd


class TestRawqGetMapPush:
    async def test_not_available(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", None)
        result = await get_map("/project")
        assert result is None

    async def test_success(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        proc_result = MagicMock()
        proc_result.returncode = 0
        proc_result.stdout = b'{"files": []}'
        with patch("anyio.run_process", AsyncMock(return_value=proc_result)):
            result = await get_map("/project")
        assert result == {"files": []}

    async def test_with_lang(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        proc_result = MagicMock()
        proc_result.returncode = 0
        proc_result.stdout = b'{"files": []}'
        with patch(
            "anyio.run_process", AsyncMock(return_value=proc_result)
        ) as mock_run:
            await get_map("/project", lang_filter="rust")
        cmd = mock_run.call_args[0][0]
        assert "--lang" in cmd

    async def test_failure(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        proc_result = MagicMock()
        proc_result.returncode = 1
        proc_result.stdout = b""
        with patch("anyio.run_process", AsyncMock(return_value=proc_result)):
            result = await get_map("/project")
        assert result is None

    async def test_exception(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        with patch("anyio.run_process", AsyncMock(side_effect=OSError("fail"))):
            result = await get_map("/project")
        assert result is None


class TestRawqGetVersionPush:
    async def test_not_available(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", None)
        result = await get_version()
        assert result is None

    async def test_success(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        proc_result = MagicMock()
        proc_result.returncode = 0
        proc_result.stdout = b"rawq 0.2.1\n"
        with patch("anyio.run_process", AsyncMock(return_value=proc_result)):
            result = await get_version()
        assert result == "0.2.1"

    async def test_failure(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        proc_result = MagicMock()
        proc_result.returncode = 1
        with patch("anyio.run_process", AsyncMock(return_value=proc_result)):
            result = await get_version()
        assert result is None

    async def test_exception(self, monkeypatch):
        monkeypatch.setattr(rawq_mod, "_rawq_checked", True)
        monkeypatch.setattr(rawq_mod, "_rawq_path", "/usr/bin/rawq")
        with patch("anyio.run_process", AsyncMock(side_effect=OSError("fail"))):
            result = await get_version()
        assert result is None
