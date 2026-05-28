"""ConversationSettings — conversation별 독립 설정 테스트."""

from __future__ import annotations

import pytest
from pathlib import Path

from tunapi.tunadish.context_store import (
    ConversationContextStore,
    ConversationSettings,
)
from tunapi.context import RunContext


@pytest.fixture
def store(tmp_path: Path) -> ConversationContextStore:
    return ConversationContextStore(tmp_path / "context.json")


class TestConversationSettings:
    """ConversationSettings 데이터 모델."""

    def test_empty_to_dict(self):
        s = ConversationSettings()
        assert s.to_dict() == {}

    def test_partial_to_dict(self):
        s = ConversationSettings(engine="claude", model="sonnet-4")
        d = s.to_dict()
        assert d == {"engine": "claude", "model": "sonnet-4"}
        assert "persona" not in d
        assert "trigger_mode" not in d

    def test_copy_is_independent(self):
        s = ConversationSettings(engine="claude", model="sonnet-4")
        c = s.copy()
        c.engine = "gemini"
        assert s.engine == "claude"


class TestConversationContextStoreSettings:
    """ConversationContextStore의 conv settings API."""

    @pytest.mark.anyio
    async def test_get_default_empty(self, store: ConversationContextStore):
        s = store.get_conv_settings("nonexistent")
        assert s.engine is None
        assert s.model is None

    @pytest.mark.anyio
    async def test_update_conv_settings(self, store: ConversationContextStore):
        await store.set_context("conv1", RunContext(project="proj"))
        updated = await store.update_conv_settings(
            "conv1", engine="gemini", model="flash-2"
        )
        assert updated.engine == "gemini"
        assert updated.model == "flash-2"
        assert updated.persona is None

    @pytest.mark.anyio
    async def test_update_preserves_other_fields(self, store: ConversationContextStore):
        await store.set_context("conv1", RunContext(project="proj"))
        await store.update_conv_settings("conv1", engine="claude", persona="creative")
        await store.update_conv_settings("conv1", model="opus-4")
        s = store.get_conv_settings("conv1")
        assert s.engine == "claude"
        assert s.model == "opus-4"
        assert s.persona == "creative"

    @pytest.mark.anyio
    async def test_update_nonexistent_conv(self, store: ConversationContextStore):
        result = await store.update_conv_settings("no-such", engine="claude")
        assert result.engine is None  # no-op

    @pytest.mark.anyio
    async def test_copy_conv_settings(self, store: ConversationContextStore):
        await store.set_context("parent", RunContext(project="proj"))
        await store.set_context("child", RunContext(project="proj"))
        await store.update_conv_settings("parent", engine="claude", model="sonnet-4")

        await store.copy_conv_settings("parent", "child")
        child_s = store.get_conv_settings("child")
        assert child_s.engine == "claude"
        assert child_s.model == "sonnet-4"

        # 독립성 확인
        await store.update_conv_settings("child", model="opus-4")
        parent_s = store.get_conv_settings("parent")
        assert parent_s.model == "sonnet-4"

    @pytest.mark.anyio
    async def test_persistence(self, tmp_path: Path):
        path = tmp_path / "ctx.json"
        store1 = ConversationContextStore(path)
        await store1.set_context("c1", RunContext(project="p"))
        await store1.update_conv_settings("c1", engine="gemini", trigger_mode="all")

        # 새 인스턴스로 로드
        store2 = ConversationContextStore(path)
        s = store2.get_conv_settings("c1")
        assert s.engine == "gemini"
        assert s.trigger_mode == "all"
        assert s.model is None

    @pytest.mark.anyio
    async def test_settings_in_list_conversations(
        self, store: ConversationContextStore
    ):
        await store.set_context("c1", RunContext(project="p"))
        await store.update_conv_settings("c1", engine="claude")
        # list_conversations는 settings를 포함하지 않음 (별도 API)
        convs = store.list_conversations()
        assert len(convs) == 1
        assert convs[0]["id"] == "c1"

    @pytest.mark.anyio
    async def test_clear_removes_settings(self, store: ConversationContextStore):
        await store.set_context("c1", RunContext(project="p"))
        await store.update_conv_settings("c1", engine="claude")
        await store.clear("c1")
        s = store.get_conv_settings("c1")
        assert s.engine is None
