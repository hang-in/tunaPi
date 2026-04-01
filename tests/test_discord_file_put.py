"""Tests for /file put helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from tunapi.discord.file_transfer import DEFAULT_DENY_GLOBS, save_attachment_to_path


class FakeAttachment:
    def __init__(self, *, filename: str, payload: bytes) -> None:
        self.filename = filename
        self.size = len(payload)
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


@pytest.mark.anyio
async def test_save_attachment_to_path_refuses_overwrite_without_force(
    tmp_path,
) -> None:
    run_root = tmp_path
    (run_root / "foo.txt").write_text("old", encoding="utf-8")

    attachment = FakeAttachment(filename="bar.txt", payload=b"new")
    result = await save_attachment_to_path(
        attachment, run_root, Path("foo.txt"), DEFAULT_DENY_GLOBS, force=False
    )

    assert result.error is not None
    assert "already exists" in result.error
    assert (run_root / "foo.txt").read_text(encoding="utf-8") == "old"


@pytest.mark.anyio
async def test_save_attachment_to_path_overwrites_with_force(tmp_path) -> None:
    run_root = tmp_path
    (run_root / "foo.txt").write_text("old", encoding="utf-8")

    attachment = FakeAttachment(filename="bar.txt", payload=b"new")
    result = await save_attachment_to_path(
        attachment, run_root, Path("foo.txt"), DEFAULT_DENY_GLOBS, force=True
    )

    assert result.error is None
    assert result.overwritten is True
    assert (run_root / "foo.txt").read_bytes() == b"new"


@pytest.mark.anyio
async def test_save_attachment_to_path_denies_globs(tmp_path) -> None:
    run_root = tmp_path
    attachment = FakeAttachment(filename="x", payload=b"data")
    result = await save_attachment_to_path(
        attachment, run_root, Path(".env"), DEFAULT_DENY_GLOBS, force=True
    )
    assert result.error is not None
    assert "denied" in result.error


@pytest.mark.anyio
async def test_save_attachment_to_path_rejects_escape(tmp_path) -> None:
    run_root = tmp_path
    attachment = FakeAttachment(filename="x", payload=b"data")
    result = await save_attachment_to_path(
        attachment, run_root, Path("../escape.txt"), DEFAULT_DENY_GLOBS, force=True
    )
    assert result.error is not None
    assert "escapes" in result.error
