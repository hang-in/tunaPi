from pathlib import Path
import json

import pytest

from tunapi.model import ResumeToken
from tunapi.core.chat_sessions import ChatSessionStore


@pytest.mark.anyio
async def test_chat_sessions_store_roundtrip(tmp_path) -> None:
    path = tmp_path / "telegram_chat_sessions_state.json"
    store = ChatSessionStore(path)
    # channel_id = f"{chat_id}:{owner_id or 'chat'}"
    await store.set(
        "1:chat", ResumeToken(engine="codex", value="abc123"), cwd=Path.cwd()
    )
    await store.set("1:42", ResumeToken(engine="claude", value="res-1"), cwd=Path.cwd())

    stored_private = await store.get("1:chat", "codex", cwd=Path.cwd())
    stored_group = await store.get("1:42", "claude", cwd=Path.cwd())
    assert stored_private == ResumeToken(engine="codex", value="abc123")
    assert stored_group == ResumeToken(engine="claude", value="res-1")

    store2 = ChatSessionStore(path)
    stored_private_2 = await store2.get("1:chat", "codex", cwd=Path.cwd())
    stored_group_2 = await store2.get("1:42", "claude", cwd=Path.cwd())
    assert stored_private_2 == ResumeToken(engine="codex", value="abc123")
    assert stored_group_2 == ResumeToken(engine="claude", value="res-1")


@pytest.mark.anyio
async def test_chat_sessions_store_clear(tmp_path) -> None:
    path = tmp_path / "telegram_chat_sessions_state.json"
    store = ChatSessionStore(path)
    await store.set("2:chat", ResumeToken(engine="codex", value="one"), cwd=Path.cwd())
    await store.set("2:77", ResumeToken(engine="codex", value="two"), cwd=Path.cwd())

    await store.clear("2:chat")
    assert await store.get("2:chat", "codex", cwd=Path.cwd()) is None
    assert await store.get("2:77", "codex", cwd=Path.cwd()) == ResumeToken(
        engine="codex",
        value="two",
    )


@pytest.mark.anyio
async def test_chat_sessions_store_drops_sessions_on_cwd_change(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "telegram_chat_sessions_state.json"
    dir1 = tmp_path / "dir1"
    dir2 = tmp_path / "dir2"
    dir1.mkdir()
    dir2.mkdir()

    monkeypatch.chdir(dir1)
    store = ChatSessionStore(path)
    await store.set(
        "1:chat", ResumeToken(engine="codex", value="abc123"), cwd=Path.cwd()
    )
    assert await store.get("1:chat", "codex", cwd=Path.cwd()) == ResumeToken(
        engine="codex", value="abc123"
    )

    store2 = ChatSessionStore(path)
    assert await store2.sync_startup_cwd(Path.cwd()) is False
    assert await store2.get("1:chat", "codex", cwd=Path.cwd()) == ResumeToken(
        engine="codex", value="abc123"
    )

    monkeypatch.chdir(dir2)
    store3 = ChatSessionStore(path)
    assert await store3.sync_startup_cwd(Path.cwd()) is True
    assert await store3.get("1:chat", "codex", cwd=Path.cwd()) is None


@pytest.mark.anyio
async def test_telegram_sessions_migration(tmp_path) -> None:
    path = tmp_path / "telegram_chat_sessions_state.json"

    # 예전 telegram 스키마 파일 생성
    old_data = {
        "version": 1,
        "cwd": str(Path.cwd()),
        "chats": {
            "1:chat": {"sessions": {"codex": {"resume": "migrated-resume-token"}}}
        },
    }
    path.write_text(json.dumps(old_data), encoding="utf-8")

    store = ChatSessionStore(path)
    stored = await store.get("1:chat", "codex", cwd=Path.cwd())
    assert stored is not None
    assert stored.engine == "codex"
    assert stored.value == "migrated-resume-token"
