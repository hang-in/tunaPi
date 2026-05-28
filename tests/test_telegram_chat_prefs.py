import pytest
from pathlib import Path
from tunapi.core.chat_prefs import ChatPrefsStore
from tunapi.context import RunContext
from tunapi.telegram.engine_overrides import (
    EngineOverrides,
    get_telegram_engine_override,
    set_telegram_engine_override,
)


@pytest.mark.anyio
async def test_chat_prefs_store_roundtrip(tmp_path) -> None:
    path = tmp_path / "telegram_chat_prefs_state.json"
    store = ChatPrefsStore(path)
    await store.set_default_engine("123", "codex")
    await store.set_trigger_mode("123", "mentions")
    await store.set_default_engine("123", "codex")
    await store.set_default_engine("456", None)

    assert await store.get_default_engine("123") == "codex"
    assert await store.get_trigger_mode("123") == "mentions"

    store2 = ChatPrefsStore(path)
    assert await store2.get_default_engine("123") == "codex"
    assert await store2.get_trigger_mode("123") == "mentions"

    await store2.set_default_engine("123", None)
    assert await store2.get_default_engine("123") is None
    assert await store2.get_trigger_mode("123") == "mentions"

    await store2.set_trigger_mode("123", None)
    assert await store2.get_trigger_mode("123") is None


@pytest.mark.anyio
async def test_telegram_prefs_migration(tmp_path) -> None:
    import json

    path = tmp_path / "telegram_chat_prefs_state.json"

    # 예전 telegram 스키마 파일 생성
    old_data = {
        "version": 1,
        "chats": {
            "123": {
                "default_engine": "codex",
                "trigger_mode": "mentions",
                "engine_overrides": {"codex": {"model": "gpt-4", "reasoning": "high"}},
            }
        },
    }
    path.write_text(json.dumps(old_data), encoding="utf-8")

    store = ChatPrefsStore(path)
    # 마이그레이션이 잘 되었는지 검증
    assert await store.get_default_engine("123") == "codex"
    assert await store.get_trigger_mode("123") == "mentions"
    assert await store.get_engine_model("123", "codex") == "gpt-4"
    assert await store.get_engine_reasoning("123", "codex") == "high"

    # 헬퍼 함수 동작 검증
    override = await get_telegram_engine_override(store, 123, "codex")
    assert override is not None
    assert override.model == "gpt-4"
    assert override.reasoning == "high"

    await set_telegram_engine_override(
        store, 123, "codex", EngineOverrides(model="gpt-3.5", reasoning="low")
    )
    override2 = await get_telegram_engine_override(store, 123, "codex")
    assert override2 is not None
    assert override2.model == "gpt-3.5"
    assert override2.reasoning == "low"


@pytest.mark.anyio
class TestChatPrefsStore:
    async def test_default_engine(self, tmp_path: Path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        assert await store.get_default_engine("ch1") is None
        await store.set_default_engine("ch1", "claude")
        assert await store.get_default_engine("ch1") == "claude"

    async def test_engine_locked(self, tmp_path: Path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        assert not await store.is_engine_locked("ch1")
        await store.lock_engine("ch1")
        assert await store.is_engine_locked("ch1")

    async def test_set_engine_with_lock(self, tmp_path: Path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        await store.set_default_engine("ch1", "claude", lock=True)
        assert await store.is_engine_locked("ch1")

    async def test_trigger_mode(self, tmp_path: Path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        assert await store.get_trigger_mode("ch1") is None
        await store.set_trigger_mode("ch1", "mentions")
        assert await store.get_trigger_mode("ch1") == "mentions"

    async def test_context(self, tmp_path: Path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        assert await store.get_context("ch1") is None
        await store.set_context("ch1", RunContext(project="proj", branch="main"))
        ctx = await store.get_context("ch1")
        assert ctx is not None
        assert ctx.project == "proj"
        assert ctx.branch == "main"

    async def test_engine_model(self, tmp_path: Path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        assert await store.get_engine_model("ch1", "claude") is None
        await store.set_engine_model("ch1", "claude", "opus")
        assert await store.get_engine_model("ch1", "claude") == "opus"
        await store.clear_engine_model("ch1", "claude")
        assert await store.get_engine_model("ch1", "claude") is None

    async def test_clear_engine_model_nonexistent(self, tmp_path: Path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        await store.clear_engine_model("ch1", "claude")  # no-op

    async def test_get_all_engine_models(self, tmp_path: Path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        await store.set_engine_model("ch1", "claude", "opus")
        await store.set_engine_model("ch1", "codex", "gpt4")
        models = await store.get_all_engine_models("ch1")
        assert models == {"claude": "opus", "codex": "gpt4"}

    async def test_persona_crud(self, tmp_path: Path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        assert await store.get_persona("reviewer") is None
        assert await store.list_personas() == {}
        await store.add_persona("reviewer", "You are a code reviewer")
        p = await store.get_persona("reviewer")
        assert p is not None
        assert p.prompt == "You are a code reviewer"
        all_p = await store.list_personas()
        assert "reviewer" in all_p
        assert await store.remove_persona("reviewer") is True
        assert await store.remove_persona("reviewer") is False
        assert await store.get_persona("reviewer") is None
