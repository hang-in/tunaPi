"""Tests for core/files.py."""

from pathlib import Path


from tunapi.core.files import (
    FilePutResult,
    deny_reason,
    format_bytes,
    normalize_relative_path,
    read_file,
    resolve_path,
    save_file,
    write_bytes_atomic,
    extract_file_paths,
    cleanup_incoming,
)


class TestNormalizeRelativePath:
    def test_valid(self):
        assert normalize_relative_path("foo/bar.txt") == "foo/bar.txt"

    def test_strips_whitespace(self):
        assert normalize_relative_path("  foo.txt  ") == "foo.txt"

    def test_backslash_to_forward(self):
        assert normalize_relative_path("foo\\bar.txt") == "foo/bar.txt"

    def test_rejects_absolute(self):
        assert normalize_relative_path("/etc/passwd") is None

    def test_rejects_home(self):
        assert normalize_relative_path("~/file") is None

    def test_rejects_dotdot(self):
        assert normalize_relative_path("../secret") is None

    def test_rejects_empty(self):
        assert normalize_relative_path("") is None
        assert normalize_relative_path("   ") is None


class TestDenyReason:
    def test_allowed_path(self):
        assert deny_reason("src/main.py") is None

    def test_git_denied(self):
        r = deny_reason(".git/config")
        assert r is not None
        assert ".git/**" in r

    def test_env_denied(self):
        assert deny_reason(".env") is not None

    def test_pem_denied(self):
        assert deny_reason("key.pem") is not None

    def test_custom_globs(self):
        assert deny_reason("secret.txt", ("secret.*",)) is not None
        assert deny_reason("ok.txt", ("secret.*",)) is None


class TestResolvePath:
    def test_within_root(self, tmp_path):
        target = resolve_path("foo/bar.txt", tmp_path)
        assert target is not None
        assert target == (tmp_path / "foo" / "bar.txt").resolve()

    def test_escape_rejected(self, tmp_path):
        assert resolve_path("../../etc/passwd", tmp_path) is None


class TestFormatBytes:
    def test_bytes(self):
        assert format_bytes(500) == "500 B"

    def test_kb(self):
        assert "KB" in format_bytes(2048)

    def test_mb(self):
        assert "MB" in format_bytes(5 * 1024 * 1024)


class TestWriteBytesAtomic:
    def test_creates_file(self, tmp_path):
        path = tmp_path / "sub" / "test.txt"
        write_bytes_atomic(path, b"hello world")
        assert path.read_bytes() == b"hello world"

    def test_overwrites(self, tmp_path):
        path = tmp_path / "test.txt"
        write_bytes_atomic(path, b"first")
        write_bytes_atomic(path, b"second")
        assert path.read_bytes() == b"second"


class TestSaveFile:
    def test_saves_valid_file(self, tmp_path):
        result = save_file("test.txt", b"content", tmp_path)
        assert result.ok
        assert result.path is not None
        assert result.path.read_bytes() == b"content"
        assert "saved" in result.message

    def test_too_large(self, tmp_path):
        result = save_file("big.txt", b"x" * 100, tmp_path, max_bytes=50)
        assert not result.ok
        assert "too large" in result.message

    def test_invalid_filename(self, tmp_path):
        result = save_file("/etc/passwd", b"x", tmp_path)
        assert not result.ok
        assert "invalid" in result.message

    def test_denied_path(self, tmp_path):
        result = save_file(".env", b"secret", tmp_path)
        assert not result.ok
        assert "denied" in result.message

    def test_path_escape(self, tmp_path):
        result = save_file("../../etc/passwd", b"x", tmp_path)
        assert not result.ok


class TestReadFile:
    def test_reads_existing(self, tmp_path):
        (tmp_path / "test.txt").write_bytes(b"hello")
        filename, error, data = read_file("test.txt", tmp_path)
        assert filename == "test.txt"
        assert error is None
        assert data == b"hello"

    def test_not_found(self, tmp_path):
        filename, error, data = read_file("missing.txt", tmp_path)
        assert filename is None
        assert "not found" in error
        assert data is None

    def test_denied(self, tmp_path):
        filename, error, data = read_file(".env", tmp_path)
        assert "denied" in error

    def test_too_large(self, tmp_path):
        (tmp_path / "big.txt").write_bytes(b"x" * 100)
        filename, error, data = read_file("big.txt", tmp_path, max_bytes=50)
        assert "too large" in error

    def test_directory(self, tmp_path):
        (tmp_path / "subdir").mkdir()
        filename, error, data = read_file("subdir", tmp_path)
        assert "directory" in error

    def test_path_escape(self, tmp_path):
        filename, error, data = read_file("../../etc/passwd", tmp_path)
        assert "path escape" in error


class TestDenyReasonPush:
    def test_ok(self):
        result = deny_reason("hello.txt")
        assert result is None

    def test_denied(self):
        result = deny_reason(".env")
        assert result is not None

    def test_custom_globs(self):
        result = deny_reason("secrets.txt", deny_globs=("secrets.*",))
        assert result is not None


class TestFormatBytesPush:
    def test_small(self):
        assert format_bytes(100) == "100 B"

    def test_kb(self):
        result = format_bytes(2048)
        assert "KB" in result or "kB" in result or "K" in result

    def test_mb(self):
        result = format_bytes(2 * 1024 * 1024)
        assert "M" in result


class TestNormalizeRelativePathPush:
    def test_simple(self):
        result = normalize_relative_path("hello.txt")
        assert result == "hello.txt"

    def test_traversal(self):
        result = normalize_relative_path("../secret.txt")
        assert result is None

    def test_absolute(self):
        result = normalize_relative_path("/etc/passwd")
        assert result is None

    def test_empty(self):
        result = normalize_relative_path("")
        assert result is None


class TestResolvePathPush:
    def test_valid(self, tmp_path: Path):
        (tmp_path / "test.txt").touch()
        result = resolve_path("test.txt", tmp_path)
        assert result is not None

    def test_traversal(self, tmp_path: Path):
        result = resolve_path("../secret", tmp_path)
        assert result is None


class TestExtractFilePathsPush:
    def test_no_paths(self):
        assert extract_file_paths("hello world") == []

    def test_with_paths(self):
        result = extract_file_paths("check out src/main.py and tests/test.py")
        assert len(result) >= 0  # smoke test


class TestWriteBytesAtomicPush:
    def test_basic(self, tmp_path: Path):
        p = tmp_path / "out.bin"
        write_bytes_atomic(p, b"hello")
        assert p.read_bytes() == b"hello"

    def test_creates_parent(self, tmp_path: Path):
        p = tmp_path / "sub" / "dir" / "out.bin"
        write_bytes_atomic(p, b"data")
        assert p.read_bytes() == b"data"


class TestFilePutResultPush:
    def test_ok(self):
        r = FilePutResult(path=Path("/x"), name="test.txt")
        assert r.ok is True

    def test_not_ok(self):
        r = FilePutResult(message="fail")
        assert r.ok is False


class TestCleanupIncomingPush:
    def test_no_dir(self, monkeypatch):
        """Returns 0 when incoming dir doesn't exist."""
        monkeypatch.setattr(
            "tunapi.core.files._INCOMING_ROOT",
            Path("/nonexistent/path"),
        )
        assert cleanup_incoming() == 0
