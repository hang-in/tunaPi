"""Tests for the unified ProjectSessionStore."""

from pathlib import Path

import pytest

from tunapi.core.project_sessions import ProjectSessionStore
from tunapi.model import ResumeToken


@pytest.mark.anyio
async def test_roundtrip(tmp_path: Path) -> None:
    store = ProjectSessionStore(tmp_path / "sessions.json")
    cwd = tmp_path / "repo"
    cwd.mkdir()

    await store.set("myproject", ResumeToken(engine="claude", value="tok1"), cwd=cwd)
    result = await store.get("myproject", cwd=cwd)
    assert result == ResumeToken(engine="claude", value="tok1")


@pytest.mark.anyio
async def test_case_insensitive_key(tmp_path: Path) -> None:
    store = ProjectSessionStore(tmp_path / "sessions.json")
    await store.set("MyProject", ResumeToken(engine="claude", value="tok1"))
    assert await store.get("myproject") == ResumeToken(engine="claude", value="tok1")
    assert await store.get("MYPROJECT") == ResumeToken(engine="claude", value="tok1")


@pytest.mark.anyio
async def test_clear(tmp_path: Path) -> None:
    store = ProjectSessionStore(tmp_path / "sessions.json")
    await store.set("proj", ResumeToken(engine="claude", value="tok1"))
    assert await store.has_active("proj") is True

    await store.clear("proj")
    assert await store.get("proj") is None
    assert await store.has_active("proj") is False


@pytest.mark.anyio
async def test_get_engine(tmp_path: Path) -> None:
    store = ProjectSessionStore(tmp_path / "sessions.json")
    await store.set("proj", ResumeToken(engine="gemini", value="tok1"))
    assert await store.get_engine("proj") == "gemini"
    assert await store.get_engine("nonexistent") is None


@pytest.mark.anyio
async def test_cwd_mismatch_clears(tmp_path: Path) -> None:
    store = ProjectSessionStore(tmp_path / "sessions.json")
    dir1 = tmp_path / "repo1"
    dir2 = tmp_path / "repo2"
    dir1.mkdir()
    dir2.mkdir()

    await store.set("proj", ResumeToken(engine="claude", value="tok1"), cwd=dir1)
    # Different cwd → should clear and return None
    result = await store.get("proj", cwd=dir2)
    assert result is None
    assert await store.has_active("proj") is False


@pytest.mark.anyio
async def test_cwd_none_ignores_stored_cwd(tmp_path: Path) -> None:
    """When get() is called without cwd, stored cwd is not checked."""
    store = ProjectSessionStore(tmp_path / "sessions.json")
    cwd = tmp_path / "repo"
    cwd.mkdir()

    await store.set("proj", ResumeToken(engine="claude", value="tok1"), cwd=cwd)
    # No cwd specified → should return token regardless of stored cwd
    result = await store.get("proj")
    assert result == ResumeToken(engine="claude", value="tok1")


@pytest.mark.anyio
async def test_persistence(tmp_path: Path) -> None:
    """Data survives store recreation (file-backed persistence)."""
    path = tmp_path / "sessions.json"
    store1 = ProjectSessionStore(path)
    await store1.set("proj", ResumeToken(engine="claude", value="tok1"))

    store2 = ProjectSessionStore(path)
    result = await store2.get("proj")
    assert result == ResumeToken(engine="claude", value="tok1")


@pytest.mark.anyio
async def test_multiple_projects(tmp_path: Path) -> None:
    store = ProjectSessionStore(tmp_path / "sessions.json")
    await store.set("proj-a", ResumeToken(engine="claude", value="a"))
    await store.set("proj-b", ResumeToken(engine="gemini", value="b"))

    assert await store.get("proj-a") == ResumeToken(engine="claude", value="a")
    assert await store.get("proj-b") == ResumeToken(engine="gemini", value="b")

    await store.clear("proj-a")
    assert await store.get("proj-a") is None
    assert await store.get("proj-b") == ResumeToken(engine="gemini", value="b")


@pytest.mark.anyio
async def test_cross_transport_shared(tmp_path: Path) -> None:
    """Both transports share the same sessions.json → same token."""
    path = tmp_path / "sessions.json"

    # Mattermost writes
    mm_store = ProjectSessionStore(path)
    await mm_store.set("proj", ResumeToken(engine="claude", value="shared-tok"))

    # Slack reads
    slack_store = ProjectSessionStore(path)
    result = await slack_store.get("proj")
    assert result == ResumeToken(engine="claude", value="shared-tok")


@pytest.mark.anyio
class TestProjectSessionStorePush:
    async def test_set_and_get(self, tmp_path: Path):
        store = ProjectSessionStore(tmp_path / "proj.json")
        await store.set("proj", ResumeToken(engine="claude", value="tok"))
        got = await store.get("proj")
        assert got is not None
        assert got.value == "tok"

    async def test_get_missing(self, tmp_path: Path):
        store = ProjectSessionStore(tmp_path / "proj.json")
        assert await store.get("missing") is None

    async def test_clear(self, tmp_path: Path):
        store = ProjectSessionStore(tmp_path / "proj.json")
        await store.set("proj", ResumeToken(engine="claude", value="tok"))
        await store.clear("proj")
        assert await store.get("proj") is None

    async def test_clear_nonexistent(self, tmp_path: Path):
        store = ProjectSessionStore(tmp_path / "proj.json")
        await store.clear("nope")  # no-op

    async def test_get_engine(self, tmp_path: Path):
        store = ProjectSessionStore(tmp_path / "proj.json")
        await store.set("proj", ResumeToken(engine="codex", value="tok"))
        assert await store.get_engine("proj") == "codex"

    async def test_get_engine_missing(self, tmp_path: Path):
        store = ProjectSessionStore(tmp_path / "proj.json")
        assert await store.get_engine("missing") is None

    async def test_has_active(self, tmp_path: Path):
        store = ProjectSessionStore(tmp_path / "proj.json")
        assert not await store.has_active("proj")
        await store.set("proj", ResumeToken(engine="claude", value="tok"))
        assert await store.has_active("proj")

    async def test_case_insensitive(self, tmp_path: Path):
        store = ProjectSessionStore(tmp_path / "proj.json")
        await store.set("MyProject", ResumeToken(engine="claude", value="tok"))
        got = await store.get("myproject")
        assert got is not None

    async def test_cwd_tracking(self, tmp_path: Path):
        store = ProjectSessionStore(tmp_path / "proj.json")
        cwd = tmp_path / "work"
        cwd.mkdir()
        await store.set("proj", ResumeToken(engine="claude", value="tok"), cwd=cwd)
        got = await store.get("proj", cwd=cwd)
        assert got is not None
