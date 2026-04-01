"""Tests for file_transfer module."""

from pathlib import Path

from tunapi.discord.bridge import DiscordFilesSettings
from tunapi.discord.file_transfer import (
    DEFAULT_DENY_GLOBS,
    SaveAttachmentResult,
    default_upload_name,
    default_upload_path,
    deny_reason,
)


class TestDefaultUploadName:
    """Tests for default_upload_name function."""

    def test_simple_filename(self) -> None:
        name = default_upload_name("test.txt")
        assert name == "test.txt"

    def test_none_filename(self) -> None:
        name = default_upload_name(None)
        assert name == "upload.bin"

    def test_empty_filename(self) -> None:
        name = default_upload_name("")
        assert name == "upload.bin"

    def test_path_with_directory(self) -> None:
        name = default_upload_name("some/path/file.py")
        assert name == "file.py"


class TestDefaultUploadPath:
    """Tests for default_upload_path function."""

    def test_simple_filename(self) -> None:
        path = default_upload_path("incoming", "test.txt")
        assert path == Path("incoming/test.txt")

    def test_nested_uploads_dir(self) -> None:
        path = default_upload_path("uploads/files", "doc.pdf")
        assert path == Path("uploads/files/doc.pdf")

    def test_none_filename(self) -> None:
        path = default_upload_path("incoming", None)
        assert path == Path("incoming/upload.bin")


class TestSaveAttachmentResult:
    """Tests for SaveAttachmentResult dataclass."""

    def test_success_result(self) -> None:
        result = SaveAttachmentResult(
            rel_path=Path("incoming/file.txt"), size=1024, error=None
        )
        assert result.rel_path == Path("incoming/file.txt")
        assert result.size == 1024
        assert result.error is None

    def test_error_result(self) -> None:
        result = SaveAttachmentResult(rel_path=None, size=None, error="file too large")
        assert result.rel_path is None
        assert result.size is None
        assert result.error == "file too large"


class TestDenyReason:
    """Tests for deny_reason function."""

    def test_git_directory_denied(self) -> None:
        reason = deny_reason(Path(".git/config"), DEFAULT_DENY_GLOBS)
        assert reason is not None
        assert ".git" in reason

    def test_git_nested_denied(self) -> None:
        reason = deny_reason(Path("src/.git/objects"), DEFAULT_DENY_GLOBS)
        assert reason is not None

    def test_env_file_denied(self) -> None:
        reason = deny_reason(Path(".env"), DEFAULT_DENY_GLOBS)
        assert reason is not None

    def test_env_file_with_suffix_denied(self) -> None:
        reason = deny_reason(Path(".env.local"), DEFAULT_DENY_GLOBS)
        assert reason is not None

    def test_regular_file_allowed(self) -> None:
        reason = deny_reason(Path("src/main.py"), DEFAULT_DENY_GLOBS)
        assert reason is None

    def test_regular_txt_file_allowed(self) -> None:
        reason = deny_reason(Path("incoming/notes.txt"), DEFAULT_DENY_GLOBS)
        assert reason is None


class TestDiscordFilesSettings:
    """Tests for DiscordFilesSettings dataclass."""

    def test_default_values(self) -> None:
        settings = DiscordFilesSettings()
        assert settings.enabled is False
        assert settings.auto_put is True
        assert settings.auto_put_mode == "upload"
        assert settings.uploads_dir == "incoming"
        assert settings.max_upload_bytes == 20 * 1024 * 1024

    def test_custom_values(self) -> None:
        settings = DiscordFilesSettings(enabled=True, auto_put_mode="prompt")
        assert settings.enabled is True
        assert settings.auto_put_mode == "prompt"

    def test_custom_uploads_dir(self) -> None:
        settings = DiscordFilesSettings(uploads_dir="uploads/incoming")
        assert settings.uploads_dir == "uploads/incoming"

    def test_deny_globs_default(self) -> None:
        settings = DiscordFilesSettings()
        assert ".git/**" in settings.deny_globs
        assert ".env" in settings.deny_globs
        assert ".envrc" in settings.deny_globs
