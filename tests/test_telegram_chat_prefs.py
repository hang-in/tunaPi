import pytest

from tunapi.core.chat_prefs import ChatPrefsStore
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
