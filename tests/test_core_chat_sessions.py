from pathlib import Path

import pytest
import msgspec

from tunapi.core.chat_sessions import (
    ChatSessionStore,
    _V1Entry,
    _V1State,
    _State,
)
from tunapi.model import ResumeToken

pytestmark = pytest.mark.anyio


class TestChatSessionStore:
    async def test_set_and_get(self, tmp_path: Path):
        store = ChatSessionStore(tmp_path / "sessions.json")
        token = ResumeToken(engine="claude", value="abc123")
        await store.set("ch1", token)
        got = await store.get("ch1", "claude")
        assert got is not None
        assert got.value == "abc123"
        assert got.engine == "claude"

    async def test_get_missing_channel(self, tmp_path: Path):
        store = ChatSessionStore(tmp_path / "sessions.json")
        got = await store.get("nonexistent", "claude")
        assert got is None

    async def test_get_missing_engine(self, tmp_path: Path):
        store = ChatSessionStore(tmp_path / "sessions.json")
        token = ResumeToken(engine="claude", value="abc")
        await store.set("ch1", token)
        got = await store.get("ch1", "codex")
        assert got is None

    async def test_clear_channel(self, tmp_path: Path):
        store = ChatSessionStore(tmp_path / "sessions.json")
        await store.set("ch1", ResumeToken(engine="claude", value="a"))
        await store.set("ch1", ResumeToken(engine="codex", value="b"))
        await store.clear("ch1")
        assert await store.get("ch1", "claude") is None
        assert await store.get("ch1", "codex") is None

    async def test_clear_nonexistent(self, tmp_path: Path):
        store = ChatSessionStore(tmp_path / "sessions.json")
        await store.clear("nope")  # should not raise

    async def test_clear_engine(self, tmp_path: Path):
        store = ChatSessionStore(tmp_path / "sessions.json")
        await store.set("ch1", ResumeToken(engine="claude", value="a"))
        await store.set("ch1", ResumeToken(engine="codex", value="b"))
        await store.clear_engine("ch1", "claude")
        assert await store.get("ch1", "claude") is None
        assert await store.get("ch1", "codex") is not None

    async def test_clear_engine_removes_empty_channel(self, tmp_path: Path):
        store = ChatSessionStore(tmp_path / "sessions.json")
        await store.set("ch1", ResumeToken(engine="claude", value="a"))
        await store.clear_engine("ch1", "claude")
        assert not await store.has_any("ch1")

    async def test_clear_engine_missing_channel(self, tmp_path: Path):
        store = ChatSessionStore(tmp_path / "sessions.json")
        await store.clear_engine("nope", "claude")  # no-op

    async def test_clear_engine_missing_engine(self, tmp_path: Path):
        store = ChatSessionStore(tmp_path / "sessions.json")
        await store.set("ch1", ResumeToken(engine="claude", value="a"))
        await store.clear_engine("ch1", "codex")  # no-op
        assert await store.has_any("ch1")

    async def test_has_any(self, tmp_path: Path):
        store = ChatSessionStore(tmp_path / "sessions.json")
        assert not await store.has_any("ch1")
        await store.set("ch1", ResumeToken(engine="claude", value="a"))
        assert await store.has_any("ch1")

    async def test_cwd_mismatch_clears(self, tmp_path: Path):
        store = ChatSessionStore(tmp_path / "sessions.json")
        cwd1 = tmp_path / "proj1"
        cwd1.mkdir()
        cwd2 = tmp_path / "proj2"
        cwd2.mkdir()
        await store.set("ch1", ResumeToken(engine="claude", value="a"), cwd=cwd1)
        # Get with different cwd -> should return None and clean up
        got = await store.get("ch1", "claude", cwd=cwd2)
        assert got is None

    async def test_cwd_match(self, tmp_path: Path):
        store = ChatSessionStore(tmp_path / "sessions.json")
        cwd = tmp_path / "proj"
        cwd.mkdir()
        await store.set("ch1", ResumeToken(engine="claude", value="a"), cwd=cwd)
        got = await store.get("ch1", "claude", cwd=cwd)
        assert got is not None
        assert got.value == "a"

    async def test_cwd_none_stored_but_requested(self, tmp_path: Path):
        """If stored cwd is None but requested cwd is not, session should be cleared."""
        store = ChatSessionStore(tmp_path / "sessions.json")
        cwd = tmp_path / "proj"
        cwd.mkdir()
        await store.set("ch1", ResumeToken(engine="claude", value="a"))
        got = await store.get("ch1", "claude", cwd=cwd)
        assert got is None

    async def test_v1_migration_on_load(self, tmp_path: Path):
        path = tmp_path / "sessions.json"
        v1 = _V1State(
            version=1,
            sessions={"ch1": _V1Entry(engine="claude", value="migrated")},
        )
        path.write_bytes(msgspec.json.encode(v1))
        store = ChatSessionStore(path)
        got = await store.get("ch1", "claude")
        assert got is not None
        assert got.value == "migrated"

    async def test_corrupt_file_fallback(self, tmp_path: Path):
        path = tmp_path / "sessions.json"
        path.write_text("not valid json {{{")
        store = ChatSessionStore(path)
        got = await store.get("ch1", "claude")
        assert got is None

    async def test_version_mismatch_non_v1(self, tmp_path: Path):
        path = tmp_path / "sessions.json"
        state = _State(version=999, channels={})
        path.write_bytes(msgspec.json.encode(state))
        store = ChatSessionStore(path)
        got = await store.get("ch1", "claude")
        assert got is None


async def test_chat_sessions_store_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "mattermost_sessions.json"
    store = ChatSessionStore(path)
    cwd = tmp_path / "repo"
    cwd.mkdir()

    await store.set("chan-1", ResumeToken(engine="claude", value="abc123"), cwd=cwd)

    stored = await store.get("chan-1", "claude", cwd=cwd)
    assert stored == ResumeToken(engine="claude", value="abc123")


async def test_chat_sessions_store_drops_resume_on_cwd_change(tmp_path: Path) -> None:
    path = tmp_path / "mattermost_sessions.json"
    dir1 = tmp_path / "repo1"
    dir2 = tmp_path / "repo2"
    dir1.mkdir()
    dir2.mkdir()

    store = ChatSessionStore(path)
    await store.set("chan-1", ResumeToken(engine="claude", value="abc123"), cwd=dir1)
    assert await store.get("chan-1", "claude", cwd=dir1) == ResumeToken(
        engine="claude",
        value="abc123",
    )

    store2 = ChatSessionStore(path)
    assert await store2.get("chan-1", "claude", cwd=dir2) is None
    assert await store2.has_any("chan-1") is False
