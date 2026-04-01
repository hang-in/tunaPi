"""File transfer utilities for Discord transport."""

from __future__ import annotations

import io
import os
import shlex
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

__all__ = [
    "PutAttachmentResult",
    "SaveAttachmentResult",
    "ZipTooLargeError",
    "default_upload_name",
    "default_upload_path",
    "deny_reason",
    "format_bytes",
    "normalize_relative_path",
    "parse_file_command",
    "resolve_path_within_root",
    "save_attachment",
    "save_attachment_to_path",
    "write_bytes_atomic",
    "zip_directory",
]

# Discord attachment size limit (25MB for non-nitro servers)
MAX_FILE_SIZE = 25 * 1024 * 1024

# Default deny patterns
DEFAULT_DENY_GLOBS = (".git/**", "*.env", ".env.*", "**/.env", "**/credentials*")


def split_command_args(text: str) -> tuple[str, ...]:
    """Split command arguments, handling quoted strings."""
    if not text.strip():
        return ()
    try:
        return tuple(shlex.split(text))
    except ValueError:
        return tuple(text.split())


def file_usage() -> str:
    """Return usage string for file command."""
    return "usage: `/file get <path>` or reply with attachment for `/file put <path>`"


def parse_file_command(args_text: str) -> tuple[str | None, str, str | None]:
    """Parse file command arguments.

    Returns: (command, path, error)
    """
    tokens = split_command_args(args_text)
    if not tokens:
        return None, "", file_usage()
    command = tokens[0].lower()
    rest = " ".join(tokens[1:]).strip()
    if command not in {"put", "get"}:
        return None, rest, file_usage()
    return command, rest, None


def normalize_relative_path(value: str) -> Path | None:
    """Normalize a relative path, rejecting unsafe paths."""
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.startswith("~"):
        return None
    path = Path(cleaned)
    if path.is_absolute():
        return None
    parts = [part for part in path.parts if part not in {"", "."}]
    if not parts:
        return None
    if ".." in parts:
        return None
    if ".git" in parts:
        return None
    return Path(*parts)


def resolve_path_within_root(root: Path, rel_path: Path) -> Path | None:
    """Resolve a relative path within a root directory.

    Returns None if the resolved path escapes the root.
    """
    root_resolved = root.resolve(strict=False)
    target = (root / rel_path).resolve(strict=False)
    if not target.is_relative_to(root_resolved):
        return None
    return target


def deny_reason(rel_path: Path, deny_globs: Sequence[str]) -> str | None:
    """Check if a path is denied by any glob pattern.

    Returns the matching pattern if denied, None if allowed.
    """
    if ".git" in rel_path.parts:
        return ".git/**"
    posix = PurePosixPath(rel_path.as_posix())
    for pattern in deny_globs:
        if posix.match(pattern):
            return pattern
    return None


def format_bytes(value: int) -> str:
    """Format a byte count as a human-readable string."""
    size = max(0.0, float(value))
    units = ("b", "kb", "mb", "gb", "tb")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "b":
                return f"{int(size)} b"
            if size < 10:
                return f"{size:.1f} {unit}"
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{int(size)} B"


def default_upload_name(filename: str | None) -> str:
    """Generate a default upload filename."""
    name = Path(filename or "").name if filename else ""
    if not name:
        name = "upload.bin"
    return name


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    """Write bytes to a file atomically using a temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", delete=False, dir=path.parent, prefix=".tunapi-upload-"
    ) as handle:
        handle.write(payload)
        temp_name = handle.name
    Path(temp_name).replace(path)


class ZipTooLargeError(Exception):
    """Raised when a zip file exceeds the size limit."""

    pass


def zip_directory(
    root: Path,
    rel_path: Path,
    deny_globs: Sequence[str],
    *,
    max_bytes: int | None = None,
) -> bytes:
    """Zip a directory and return the bytes.

    Args:
        root: The root directory
        rel_path: Relative path to the directory to zip
        deny_globs: Glob patterns to exclude
        max_bytes: Maximum size of the zip file

    Raises:
        ZipTooLargeError: If the zip exceeds max_bytes
    """
    target = root / rel_path
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for dirpath, _, filenames in os.walk(target, followlinks=False):
            dir_path = Path(dirpath)
            for filename in filenames:
                item = dir_path / filename
                if item.is_symlink():
                    continue
                if not item.is_file():
                    continue
                rel_item = rel_path / item.relative_to(target)
                if deny_reason(rel_item, deny_globs) is not None:
                    continue
                archive.write(item, arcname=rel_item.as_posix())
                if max_bytes is not None and buffer.tell() > max_bytes:
                    raise ZipTooLargeError()
    payload = buffer.getvalue()
    if max_bytes is not None and len(payload) > max_bytes:
        raise ZipTooLargeError()
    return payload


def default_upload_path(uploads_dir: str, filename: str | None) -> Path:
    """Generate the default upload path for a file.

    Args:
        uploads_dir: The uploads directory (e.g., "incoming")
        filename: The original filename

    Returns:
        Relative path like "incoming/filename.txt"
    """
    name = default_upload_name(filename)
    return Path(uploads_dir) / name


@dataclass(slots=True)
class SaveAttachmentResult:
    """Result of saving an attachment."""

    rel_path: Path | None
    size: int | None
    error: str | None


@dataclass(slots=True)
class PutAttachmentResult:
    """Result of saving an attachment to an explicit path."""

    rel_path: Path | None
    size: int | None
    overwritten: bool = False
    error: str | None = None


async def save_attachment(
    attachment: discord.Attachment,
    run_root: Path,
    uploads_dir: str,
    deny_globs: Sequence[str],
    *,
    max_bytes: int = 20 * 1024 * 1024,
) -> SaveAttachmentResult:
    """Save a Discord attachment to the project directory.

    Args:
        attachment: The Discord attachment to save
        run_root: The project root directory
        uploads_dir: Relative directory for uploads (e.g., "incoming")
        deny_globs: Glob patterns to deny
        max_bytes: Maximum file size in bytes

    Returns:
        SaveAttachmentResult with rel_path and size on success, or error on failure
    """
    filename = attachment.filename
    name = default_upload_name(filename)

    # Check file size
    if attachment.size > max_bytes:
        return SaveAttachmentResult(
            rel_path=None,
            size=None,
            error="file is too large to upload",
        )

    # Build the relative path
    rel_path = default_upload_path(uploads_dir, filename)

    # Check deny rules
    deny_rule = deny_reason(rel_path, deny_globs)
    if deny_rule is not None:
        return SaveAttachmentResult(
            rel_path=None,
            size=None,
            error=f"path denied by rule: {deny_rule}",
        )

    # Resolve target path
    target = resolve_path_within_root(run_root, rel_path)
    if target is None:
        return SaveAttachmentResult(
            rel_path=None,
            size=None,
            error="upload path escapes the project root",
        )

    # Check if file already exists as a directory
    if target.exists() and target.is_dir():
        return SaveAttachmentResult(
            rel_path=None,
            size=None,
            error=f"`{name}` is a directory",
        )
    # For auto_put, we overwrite existing files silently
    # (unlike manual /file put which may want --force)

    # Download and save the file
    try:
        payload = await attachment.read()
        write_bytes_atomic(target, payload)
    except OSError as e:
        return SaveAttachmentResult(
            rel_path=None,
            size=None,
            error=f"failed to save file: {e}",
        )

    return SaveAttachmentResult(
        rel_path=rel_path,
        size=len(payload),
        error=None,
    )


async def save_attachment_to_path(
    attachment: discord.Attachment,
    run_root: Path,
    rel_path: Path,
    deny_globs: Sequence[str],
    *,
    max_bytes: int = 20 * 1024 * 1024,
    force: bool = False,
) -> PutAttachmentResult:
    """Save a Discord attachment to an explicit repo-relative path.

    Enforces deny globs, keeps writes within the resolved run root, and refuses
    overwrites unless `force=True`.
    """
    # Check file size
    if attachment.size > max_bytes:
        return PutAttachmentResult(
            rel_path=None,
            size=None,
            overwritten=False,
            error="file is too large to upload",
        )

    # Check deny rules
    deny_rule = deny_reason(rel_path, deny_globs)
    if deny_rule is not None:
        return PutAttachmentResult(
            rel_path=None,
            size=None,
            overwritten=False,
            error=f"path denied by rule: {deny_rule}",
        )

    # Resolve target path
    target = resolve_path_within_root(run_root, rel_path)
    if target is None:
        return PutAttachmentResult(
            rel_path=None,
            size=None,
            overwritten=False,
            error="upload path escapes the project root",
        )

    # Existing path checks
    if target.exists() and target.is_dir():
        return PutAttachmentResult(
            rel_path=None,
            size=None,
            overwritten=False,
            error=f"`{rel_path.as_posix()}` is a directory",
        )
    overwritten = target.exists()
    if overwritten and not force:
        return PutAttachmentResult(
            rel_path=None,
            size=None,
            overwritten=False,
            error="file already exists (use force to overwrite)",
        )

    # Download and save the file
    try:
        payload = await attachment.read()
        target.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(target, payload)
    except OSError as e:
        return PutAttachmentResult(
            rel_path=None,
            size=None,
            overwritten=False,
            error=f"failed to save file: {e}",
        )

    return PutAttachmentResult(
        rel_path=rel_path,
        size=len(payload),
        overwritten=overwritten,
        error=None,
    )
