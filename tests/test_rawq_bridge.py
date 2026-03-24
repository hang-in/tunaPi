"""Tests for tunadish/rawq_bridge.py — rawq CLI bridge."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tunapi.tunadish.rawq_bridge import (
    _DEFAULT_EXCLUDE,
    _find_rawq,
    _get_rawq,
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
        with patch.dict("os.environ", {"RAWQ_BIN": ""}):
            with patch("shutil.which", return_value="/usr/bin/rawq"):
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
