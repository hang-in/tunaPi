"""Shared file transfer utilities (upload/download validation and I/O).

Transport-specific download/upload logic lives in each transport package;
this module provides the pure validation, path resolution, and atomic
write helpers that all transports share.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..logging import get_logger

logger = get_logger(__name__)

_CONFIG_DIR = Path.home() / ".tunapi"
_INCOMING_ROOT = _CONFIG_DIR / "incoming"


def resolve_incoming_dir(project: str) -> Path:
    """Return ~/.tunapi/incoming/{project}/, creating it if needed."""
    d = _INCOMING_ROOT / project
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_incoming(*, max_age_days: int = 7) -> int:
    """Delete files older than *max_age_days* under ~/.tunapi/incoming/.

    Returns number of deleted files.
    """
    if not _INCOMING_ROOT.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    deleted = 0
    for f in _INCOMING_ROOT.rglob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
            deleted += 1
    # Remove empty project dirs
    for d in _INCOMING_ROOT.iterdir():
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    if deleted:
        logger.info("incoming.cleanup", deleted=deleted, max_age_days=max_age_days)
    return deleted


_FILE_PATH_RE = re.compile(
    r"(?:^|[\s`(])(/[\w./-]+\.(?:md|txt|rst|toml|yaml|yml|json|csv))(?=[\s`),.:;!?\]]|$)",
    re.MULTILINE,
)

_MAX_ATTACH_SIZE = 20 * 1024 * 1024  # 20 MB


def extract_file_paths(text: str) -> list[Path]:
    """Extract readable local file paths from response text.

    Returns deduplicated list of existing, readable files.
    """
    seen: set[str] = set()
    result: list[Path] = []
    for m in _FILE_PATH_RE.finditer(text):
        raw = m.group(1)
        if raw in seen:
            continue
        seen.add(raw)
        p = Path(raw)
        if p.is_file() and p.stat().st_size <= _MAX_ATTACH_SIZE:
            result.append(p)
    return result


@dataclass(frozen=True, slots=True)
class FilePutResult:
    message: str = ""
    path: Path | None = None
    name: str | None = None

    @property
    def ok(self) -> bool:
        return self.path is not None


DEFAULT_DENY_GLOBS = (
    ".git/**",
    ".env",
    ".envrc",
    "*.pem",
    ".ssh/**",
)


def normalize_relative_path(value: str) -> str | None:
    """Validate and normalize a relative path. Returns None if invalid."""
    if not value or not value.strip():
        return None
    path = value.strip().replace("\\", "/")
    if path.startswith("/") or path.startswith("~"):
        return None
    if ".." in path.split("/"):
        return None
    return path


def deny_reason(
    rel_path: str,
    deny_globs: tuple[str, ...] = DEFAULT_DENY_GLOBS,
) -> str | None:
    """Return a reason string if the path should be denied, else None."""
    posix = PurePosixPath(rel_path)
    for glob in deny_globs:
        if posix.match(glob):
            return f"path matches deny pattern `{glob}`"
    return None


def resolve_path(rel_path: str, root: Path) -> Path | None:
    """Resolve a relative path within root. Returns None if it escapes."""
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root.resolve()):
        return None
    return target


def format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Atomically write bytes to a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        os.close(fd) if not os.get_inheritable(fd) else None
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def save_file(
    filename: str,
    data: bytes,
    target_dir: Path,
    *,
    deny_globs: tuple[str, ...] = DEFAULT_DENY_GLOBS,
    max_bytes: int = 20 * 1024 * 1024,
) -> FilePutResult:
    """Validate and save already-downloaded file bytes to *target_dir*.

    Returns a :class:`FilePutResult` indicating success or failure.
    """
    if len(data) > max_bytes:
        return FilePutResult(
            message=f"file too large ({format_bytes(len(data))}, max {format_bytes(max_bytes)})",
        )

    rel = normalize_relative_path(filename)
    if rel is None:
        return FilePutResult(message=f"invalid filename `{filename}`")

    reason = deny_reason(rel, deny_globs)
    if reason:
        return FilePutResult(message=f"denied: `{filename}` — {reason}")

    target = resolve_path(rel, target_dir)
    if target is None:
        return FilePutResult(message=f"path escape: `{filename}`")

    write_bytes_atomic(target, data)
    msg = f"saved `{rel}` ({format_bytes(len(data))})"
    logger.info("file.put", filename=rel, size=len(data), path=str(target))
    return FilePutResult(message=msg, path=target, name=rel)


def read_file(
    rel_path: str,
    root: Path,
    *,
    deny_globs: tuple[str, ...] = DEFAULT_DENY_GLOBS,
    max_bytes: int = 50 * 1024 * 1024,
) -> tuple[str | None, str | None, bytes | None]:
    """Read a file from *root* for download.

    Returns ``(filename, None, bytes)`` on success or
    ``(None, error_msg, None)`` on failure.
    """
    reason = deny_reason(rel_path, deny_globs)
    if reason:
        return None, f"denied: `{rel_path}` — {reason}", None

    target = resolve_path(rel_path, root)
    if target is None:
        return None, f"path escape: `{rel_path}`", None

    if not target.exists():
        return None, f"not found: `{rel_path}`", None

    if target.is_dir():
        return None, f"`{rel_path}` is a directory (zip not yet supported)", None

    size = target.stat().st_size
    if size > max_bytes:
        return (
            None,
            f"too large: {format_bytes(size)} (max {format_bytes(max_bytes)})",
            None,
        )

    data = target.read_bytes()
    filename = target.name
    logger.info("file.get", filename=rel_path, size=len(data))
    return filename, None, data
