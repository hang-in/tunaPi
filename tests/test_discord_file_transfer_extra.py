"""Extra tests for file_transfer module — save_attachment, format_bytes, deny_reason, edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from tunapi.discord.file_transfer import (
    DEFAULT_DENY_GLOBS,
    PutAttachmentResult,
    ZipTooLargeError,
    default_upload_name,
    deny_reason,
    format_bytes,
    normalize_relative_path,
    parse_file_command,
    resolve_path_within_root,
    save_attachment,
    save_attachment_to_path,
    split_command_args,
    write_bytes_atomic,
    zip_directory,
)


# ---------------------------------------------------------------------------
# FakeAttachment — mimics discord.Attachment
# ---------------------------------------------------------------------------

class FakeAttachment:
    def __init__(self, *, filename: str, payload: bytes, size: int | None = None) -> None:
        self.filename = filename
        self._payload = payload
        self.size = size if size is not None else len(payload)

    async def read(self) -> bytes:
        return self._payload


class FakeAttachmentOSError(FakeAttachment):
    """Attachment whose read() raises OSError."""

    async def read(self) -> bytes:
        raise OSError("simulated read error")


# ===========================================================================
# format_bytes
# ===========================================================================

class TestFormatBytes:
    def test_zero(self) -> None:
        assert format_bytes(0) == "0 b"

    def test_negative_clamped_to_zero(self) -> None:
        assert format_bytes(-100) == "0 b"

    def test_bytes_range(self) -> None:
        assert format_bytes(512) == "512 b"

    def test_one_kb(self) -> None:
        assert format_bytes(1024) == "1.0 kb"

    def test_large_kb(self) -> None:
        assert format_bytes(10240) == "10 kb"

    def test_megabytes(self) -> None:
        assert format_bytes(1024 * 1024) == "1.0 mb"

    def test_gigabytes(self) -> None:
        assert format_bytes(1024 ** 3) == "1.0 gb"

    def test_terabytes(self) -> None:
        assert format_bytes(1024 ** 4) == "1.0 tb"

    def test_beyond_terabytes(self) -> None:
        # 2 TB — should still format as tb (last unit)
        result = format_bytes(2 * 1024 ** 4)
        assert "tb" in result

    def test_fractional_kb(self) -> None:
        # 1.5 KB = 1536 bytes
        assert format_bytes(1536) == "1.5 kb"

    def test_just_under_10_kb(self) -> None:
        # 9.5 KB — should show one decimal
        result = format_bytes(int(9.5 * 1024))
        assert "." in result
        assert "kb" in result


# ===========================================================================
# deny_reason — extra edge cases
# ===========================================================================

class TestDenyReasonExtra:
    def test_credentials_file_denied(self) -> None:
        # Top-level credentials.json — use explicit glob to test deny_reason logic
        reason = deny_reason(Path("credentials.json"), ("credentials*",))
        assert reason is not None

    def test_nested_credentials_denied(self) -> None:
        reason = deny_reason(Path("config/credentials.yaml"), DEFAULT_DENY_GLOBS)
        assert reason is not None

    def test_nested_env_file_denied(self) -> None:
        reason = deny_reason(Path("src/.env"), DEFAULT_DENY_GLOBS)
        assert reason is not None

    def test_custom_deny_glob(self) -> None:
        reason = deny_reason(Path("secret.key"), ("*.key",))
        assert reason == "*.key"

    def test_no_match_returns_none(self) -> None:
        reason = deny_reason(Path("hello.txt"), ())
        assert reason is None

    def test_git_in_parts_always_denied(self) -> None:
        # .git in parts is caught before glob matching
        reason = deny_reason(Path("a/.git/HEAD"), ())
        assert reason == ".git/**"


# ===========================================================================
# normalize_relative_path — edge cases
# ===========================================================================

class TestNormalizeRelativePath:
    def test_empty_string(self) -> None:
        assert normalize_relative_path("") is None

    def test_whitespace_only(self) -> None:
        assert normalize_relative_path("   ") is None

    def test_tilde_rejected(self) -> None:
        assert normalize_relative_path("~/file.txt") is None

    def test_absolute_rejected(self) -> None:
        assert normalize_relative_path("/etc/passwd") is None

    def test_dotdot_rejected(self) -> None:
        assert normalize_relative_path("../escape") is None

    def test_dotgit_rejected(self) -> None:
        assert normalize_relative_path(".git/config") is None

    def test_current_dir_only(self) -> None:
        assert normalize_relative_path(".") is None

    def test_valid_simple(self) -> None:
        result = normalize_relative_path("src/main.py")
        assert result == Path("src/main.py")

    def test_strips_whitespace(self) -> None:
        result = normalize_relative_path("  foo.txt  ")
        assert result == Path("foo.txt")

    def test_removes_dot_segments(self) -> None:
        result = normalize_relative_path("./src/./main.py")
        assert result == Path("src/main.py")


# ===========================================================================
# resolve_path_within_root
# ===========================================================================

class TestResolvePathWithinRoot:
    def test_valid_path(self, tmp_path: Path) -> None:
        result = resolve_path_within_root(tmp_path, Path("sub/file.txt"))
        assert result is not None
        assert result.is_relative_to(tmp_path)

    def test_escape_rejected(self, tmp_path: Path) -> None:
        result = resolve_path_within_root(tmp_path, Path("../../etc/passwd"))
        assert result is None


# ===========================================================================
# split_command_args
# ===========================================================================

class TestSplitCommandArgs:
    def test_empty(self) -> None:
        assert split_command_args("") == ()

    def test_whitespace(self) -> None:
        assert split_command_args("   ") == ()

    def test_simple(self) -> None:
        assert split_command_args("get file.txt") == ("get", "file.txt")

    def test_quoted(self) -> None:
        assert split_command_args('put "path with spaces.txt"') == ("put", "path with spaces.txt")

    def test_bad_quotes_fallback(self) -> None:
        # Unmatched quote — falls back to str.split
        result = split_command_args('put "unclosed')
        assert len(result) == 2


# ===========================================================================
# parse_file_command
# ===========================================================================

class TestParseFileCommand:
    def test_empty(self) -> None:
        cmd, path, err = parse_file_command("")
        assert cmd is None
        assert err is not None

    def test_unknown_command(self) -> None:
        cmd, path, err = parse_file_command("delete foo.txt")
        assert cmd is None
        assert err is not None

    def test_get(self) -> None:
        cmd, path, err = parse_file_command("get src/main.py")
        assert cmd == "get"
        assert path == "src/main.py"
        assert err is None

    def test_put(self) -> None:
        cmd, path, err = parse_file_command("put readme.md")
        assert cmd == "put"
        assert path == "readme.md"
        assert err is None

    def test_case_insensitive(self) -> None:
        cmd, _, err = parse_file_command("GET foo")
        assert cmd == "get"
        assert err is None


# ===========================================================================
# write_bytes_atomic
# ===========================================================================

class TestWriteBytesAtomic:
    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c.txt"
        write_bytes_atomic(target, b"hello")
        assert target.read_bytes() == b"hello"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        target.write_bytes(b"old")
        write_bytes_atomic(target, b"new")
        assert target.read_bytes() == b"new"


# ===========================================================================
# default_upload_name — additional
# ===========================================================================

class TestDefaultUploadNameExtra:
    def test_dotfile(self) -> None:
        assert default_upload_name(".hidden") == ".hidden"


# ===========================================================================
# zip_directory
# ===========================================================================

class TestZipDirectory:
    def test_basic_zip(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        sub = root / "src"
        sub.mkdir(parents=True)
        (sub / "a.py").write_text("print('a')")
        payload = zip_directory(root, Path("src"), DEFAULT_DENY_GLOBS)
        assert len(payload) > 0

    def test_deny_globs_excluded(self, tmp_path: Path) -> None:
        import zipfile, io
        root = tmp_path / "project"
        sub = root / "src"
        sub.mkdir(parents=True)
        (sub / "a.py").write_text("code")
        (sub / ".env").write_text("SECRET=x")
        payload = zip_directory(root, Path("src"), DEFAULT_DENY_GLOBS)
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = zf.namelist()
        assert "src/a.py" in names
        assert "src/.env" not in names

    def test_max_bytes_raises(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        sub = root / "data"
        sub.mkdir(parents=True)
        (sub / "big.bin").write_bytes(b"x" * 10000)
        with pytest.raises(ZipTooLargeError):
            zip_directory(root, Path("data"), (), max_bytes=10)

    def test_symlinks_skipped(self, tmp_path: Path) -> None:
        import zipfile, io
        root = tmp_path / "project"
        sub = root / "src"
        sub.mkdir(parents=True)
        (sub / "real.txt").write_text("real")
        (sub / "link.txt").symlink_to(sub / "real.txt")
        payload = zip_directory(root, Path("src"), ())
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = zf.namelist()
        assert "src/real.txt" in names
        assert "src/link.txt" not in names


# ===========================================================================
# save_attachment (async)
# ===========================================================================

class TestSaveAttachment:
    @pytest.mark.anyio
    async def test_success(self, tmp_path: Path) -> None:
        run_root = tmp_path
        att = FakeAttachment(filename="hello.txt", payload=b"world")
        result = await save_attachment(att, run_root, "incoming", DEFAULT_DENY_GLOBS)
        assert result.error is None
        assert result.rel_path == Path("incoming/hello.txt")
        assert result.size == 5
        assert (run_root / "incoming" / "hello.txt").read_bytes() == b"world"

    @pytest.mark.anyio
    async def test_too_large(self, tmp_path: Path) -> None:
        att = FakeAttachment(filename="big.bin", payload=b"x", size=100)
        result = await save_attachment(att, tmp_path, "incoming", DEFAULT_DENY_GLOBS, max_bytes=50)
        assert result.error is not None
        assert "too large" in result.error

    @pytest.mark.anyio
    async def test_denied_path(self, tmp_path: Path) -> None:
        att = FakeAttachment(filename=".env", payload=b"SECRET=1")
        result = await save_attachment(att, tmp_path, ".", DEFAULT_DENY_GLOBS)
        assert result.error is not None
        assert "denied" in result.error

    @pytest.mark.anyio
    async def test_target_is_directory(self, tmp_path: Path) -> None:
        (tmp_path / "incoming" / "file.txt").mkdir(parents=True)
        att = FakeAttachment(filename="file.txt", payload=b"data")
        result = await save_attachment(att, tmp_path, "incoming", DEFAULT_DENY_GLOBS)
        assert result.error is not None
        assert "directory" in result.error

    @pytest.mark.anyio
    async def test_none_filename_uses_default(self, tmp_path: Path) -> None:
        att = FakeAttachment(filename=None, payload=b"data")
        # FakeAttachment stores None; default_upload_name handles it
        att.filename = None  # type: ignore[assignment]
        result = await save_attachment(att, tmp_path, "incoming", ())
        assert result.error is None
        assert result.rel_path is not None
        assert result.rel_path.name == "upload.bin"

    @pytest.mark.anyio
    async def test_os_error_during_write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        att = FakeAttachment(filename="ok.txt", payload=b"data")

        def bad_write(path: Path, payload: bytes) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("tunapi.discord.file_transfer.write_bytes_atomic", bad_write)
        result = await save_attachment(att, tmp_path, "incoming", ())
        assert result.error is not None
        assert "failed to save" in result.error


# ===========================================================================
# save_attachment_to_path — extra edge cases
# ===========================================================================

class TestSaveAttachmentToPathExtra:
    @pytest.mark.anyio
    async def test_too_large(self, tmp_path: Path) -> None:
        att = FakeAttachment(filename="big.bin", payload=b"x", size=100)
        result = await save_attachment_to_path(
            att, tmp_path, Path("big.bin"), DEFAULT_DENY_GLOBS, max_bytes=50
        )
        assert result.error is not None
        assert "too large" in result.error

    @pytest.mark.anyio
    async def test_target_is_directory(self, tmp_path: Path) -> None:
        (tmp_path / "mydir").mkdir()
        att = FakeAttachment(filename="x", payload=b"data")
        result = await save_attachment_to_path(
            att, tmp_path, Path("mydir"), (), force=True
        )
        assert result.error is not None
        assert "directory" in result.error

    @pytest.mark.anyio
    async def test_new_file_success(self, tmp_path: Path) -> None:
        att = FakeAttachment(filename="new.py", payload=b"print(1)")
        result = await save_attachment_to_path(
            att, tmp_path, Path("src/new.py"), ()
        )
        assert result.error is None
        assert result.overwritten is False
        assert result.size == 8
        assert (tmp_path / "src" / "new.py").read_bytes() == b"print(1)"

    @pytest.mark.anyio
    async def test_os_error_during_write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        att = FakeAttachment(filename="ok.txt", payload=b"data")

        def bad_write(path: Path, payload: bytes) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("tunapi.discord.file_transfer.write_bytes_atomic", bad_write)
        result = await save_attachment_to_path(att, tmp_path, Path("ok.txt"), ())
        assert result.error is not None
        assert "failed to save" in result.error


# ===========================================================================
# PutAttachmentResult
# ===========================================================================

class TestPutAttachmentResult:
    def test_defaults(self) -> None:
        r = PutAttachmentResult(rel_path=Path("x"), size=10)
        assert r.overwritten is False
        assert r.error is None
