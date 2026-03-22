"""Tests for tunadish/session_store.py — conv별 독립 resume token."""

from __future__ import annotations

import json

import pytest

from tunapi.tunadish.session_store import ConversationSessionStore

pytestmark = pytest.mark.anyio


class TestConversationSessionStore:
    async def test_set_and_get(self, tmp_path):
        path = tmp_path / "conv_sessions.json"
        store = ConversationSessionStore(path)

        await store.set("conv-1", engine="claude", token="tok-abc")
        entry = await store.get("conv-1")
        assert entry is not None
        assert entry.engine == "claude"
        assert entry.token == "tok-abc"

    async def test_get_missing(self, tmp_path):
        path = tmp_path / "conv_sessions.json"
        store = ConversationSessionStore(path)
        assert await store.get("nonexistent") is None

    async def test_clear(self, tmp_path):
        path = tmp_path / "conv_sessions.json"
        store = ConversationSessionStore(path)

        await store.set("conv-1", engine="claude", token="tok-abc")
        await store.clear("conv-1")
        assert await store.get("conv-1") is None

    async def test_persistence(self, tmp_path):
        path = tmp_path / "conv_sessions.json"
        store1 = ConversationSessionStore(path)
        await store1.set("conv-1", engine="gemini", token="tok-xyz", cwd="/tmp")

        # Reload from disk
        store2 = ConversationSessionStore(path)
        entry = await store2.get("conv-1")
        assert entry is not None
        assert entry.engine == "gemini"
        assert entry.token == "tok-xyz"
        assert entry.cwd == "/tmp"

    async def test_multiple_conversations(self, tmp_path):
        path = tmp_path / "conv_sessions.json"
        store = ConversationSessionStore(path)

        await store.set("conv-1", engine="claude", token="tok-1")
        await store.set("conv-2", engine="gemini", token="tok-2")

        e1 = await store.get("conv-1")
        e2 = await store.get("conv-2")
        assert e1.token == "tok-1"
        assert e2.token == "tok-2"

    async def test_overwrite(self, tmp_path):
        path = tmp_path / "conv_sessions.json"
        store = ConversationSessionStore(path)

        await store.set("conv-1", engine="claude", token="old")
        await store.set("conv-1", engine="claude", token="new")
        entry = await store.get("conv-1")
        assert entry.token == "new"

    async def test_file_format(self, tmp_path):
        path = tmp_path / "conv_sessions.json"
        store = ConversationSessionStore(path)
        await store.set("conv-1", engine="claude", token="tok")

        data = json.loads(path.read_text("utf-8"))
        assert data["version"] == 1
        assert "conv-1" in data["conversations"]
        assert data["conversations"]["conv-1"]["token"] == "tok"

    async def test_corrupt_file_ignored(self, tmp_path):
        path = tmp_path / "conv_sessions.json"
        path.write_text("not json", "utf-8")
        store = ConversationSessionStore(path)
        assert await store.get("anything") is None
