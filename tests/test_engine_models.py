"""Tests for engine_models.py and chat_prefs engine model overrides."""

from __future__ import annotations

import pytest

from tunapi.core.chat_prefs import ChatPrefsStore
from tunapi.engine_models import KNOWN_MODELS, get_models
from tunapi.transport import RenderedMessage

pytestmark = pytest.mark.anyio


# -- Registry tests --


class TestKnownModels:
    def test_claude_models(self):
        models, source = get_models("claude")
        assert source in ("discovered", "fallback")
        assert any("sonnet" in m for m in models)
        assert any("opus" in m for m in models)

    def test_gemini_models(self):
        models, source = get_models("gemini")
        # gemini may not have discoverable or fallback models
        assert source in ("discovered", "fallback", "unknown")

    def test_codex_models(self):
        models, source = get_models("codex")
        assert source in ("discovered", "fallback")
        assert len(models) > 0

    def test_unknown_engine(self):
        models, source = get_models("nonexistent")
        assert source == "unknown"
        assert models == []

    def test_all_engines_have_models(self):
        for engine in KNOWN_MODELS:
            models, source = get_models(engine)
            assert source in ("discovered", "fallback", "unknown")
            if models:
                assert source in ("discovered", "fallback")


# -- ChatPrefs engine model storage --


class TestChatPrefsEngineModel:
    async def test_get_unset_returns_none(self, tmp_path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        assert await store.get_engine_model("ch1", "claude") is None

    async def test_set_and_get(self, tmp_path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        await store.set_engine_model("ch1", "claude", "sonnet")
        assert await store.get_engine_model("ch1", "claude") == "sonnet"

    async def test_set_multiple_engines(self, tmp_path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        await store.set_engine_model("ch1", "claude", "opus")
        await store.set_engine_model("ch1", "gemini", "gemini-2.5-pro")
        assert await store.get_engine_model("ch1", "claude") == "opus"
        assert await store.get_engine_model("ch1", "gemini") == "gemini-2.5-pro"

    async def test_clear(self, tmp_path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        await store.set_engine_model("ch1", "claude", "sonnet")
        await store.clear_engine_model("ch1", "claude")
        assert await store.get_engine_model("ch1", "claude") is None

    async def test_clear_nonexistent(self, tmp_path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        # Should not raise
        await store.clear_engine_model("ch1", "claude")

    async def test_get_all_engine_models(self, tmp_path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        await store.set_engine_model("ch1", "claude", "opus")
        await store.set_engine_model("ch1", "codex", "o3")
        all_models = await store.get_all_engine_models("ch1")
        assert all_models == {"claude": "opus", "codex": "o3"}

    async def test_get_all_empty(self, tmp_path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        assert await store.get_all_engine_models("ch1") == {}

    async def test_persistence(self, tmp_path):
        path = tmp_path / "prefs.json"
        store1 = ChatPrefsStore(path)
        await store1.set_engine_model("ch1", "claude", "haiku")

        store2 = ChatPrefsStore(path)
        assert await store2.get_engine_model("ch1", "claude") == "haiku"

    async def test_channels_isolated(self, tmp_path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        await store.set_engine_model("ch1", "claude", "opus")
        await store.set_engine_model("ch2", "claude", "sonnet")
        assert await store.get_engine_model("ch1", "claude") == "opus"
        assert await store.get_engine_model("ch2", "claude") == "sonnet"

    async def test_engine_model_coexists_with_default_engine(self, tmp_path):
        store = ChatPrefsStore(tmp_path / "prefs.json")
        await store.set_default_engine("ch1", "claude")
        await store.set_engine_model("ch1", "claude", "sonnet")
        assert await store.get_default_engine("ch1") == "claude"
        assert await store.get_engine_model("ch1", "claude") == "sonnet"


# -- Command handler tests --


class _FakeRuntime:
    def available_engine_ids(self):
        return ["claude", "gemini", "codex"]

    def project_aliases(self):
        return []

    @property
    def default_engine(self):
        return "claude"


class TestHandleModelsCommand:
    async def test_all_engines(self, tmp_path):
        from tunapi.mattermost.commands import handle_models

        store = ChatPrefsStore(tmp_path / "prefs.json")
        captured: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            captured.append(msg.text)

        await handle_models(
            "",
            channel_id="ch1",
            runtime=_FakeRuntime(),
            chat_prefs=store,
            send=send,
        )

        text = captured[0]
        assert "claude" in text
        assert "gemini" in text
        assert "codex" in text
        assert any(s in text for s in ("discovered", "fallback", "unknown"))

    async def test_specific_engine(self, tmp_path):
        from tunapi.mattermost.commands import handle_models

        store = ChatPrefsStore(tmp_path / "prefs.json")
        await store.set_engine_model("ch1", "claude", "opus")
        captured: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            captured.append(msg.text)

        await handle_models(
            "claude",
            channel_id="ch1",
            runtime=_FakeRuntime(),
            chat_prefs=store,
            send=send,
        )

        text = captured[0]
        assert "claude" in text
        assert "opus" in text
        assert "current" in text
        # Should NOT contain other engines
        assert "gemini" not in text.split("claude")[0]

    async def test_unknown_engine(self, tmp_path):
        from tunapi.mattermost.commands import handle_models

        store = ChatPrefsStore(tmp_path / "prefs.json")
        captured: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            captured.append(msg.text)

        await handle_models(
            "nonexistent",
            channel_id="ch1",
            runtime=_FakeRuntime(),
            chat_prefs=store,
            send=send,
        )

        assert "Unknown engine" in captured[0]


class TestHandleModelCommand:
    async def test_set_model(self, tmp_path):
        from tunapi.mattermost.commands import handle_model

        store = ChatPrefsStore(tmp_path / "prefs.json")
        captured: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            captured.append(msg.text)

        await handle_model(
            "claude sonnet",
            channel_id="ch1",
            runtime=_FakeRuntime(),
            chat_prefs=store,
            send=send,
        )

        assert "sonnet" in captured[0]
        assert await store.get_engine_model("ch1", "claude") == "sonnet"

    async def test_clear_model(self, tmp_path):
        from tunapi.mattermost.commands import handle_model

        store = ChatPrefsStore(tmp_path / "prefs.json")
        await store.set_engine_model("ch1", "claude", "opus")
        captured: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            captured.append(msg.text)

        await handle_model(
            "claude clear",
            channel_id="ch1",
            runtime=_FakeRuntime(),
            chat_prefs=store,
            send=send,
        )

        assert "cleared" in captured[0].lower()
        assert await store.get_engine_model("ch1", "claude") is None

    async def test_engine_switch_shows_model(self, tmp_path):
        from tunapi.mattermost.commands import handle_model

        store = ChatPrefsStore(tmp_path / "prefs.json")
        await store.set_engine_model("ch1", "claude", "opus")
        captured: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            captured.append(msg.text)

        await handle_model(
            "claude",
            channel_id="ch1",
            runtime=_FakeRuntime(),
            chat_prefs=store,
            send=send,
        )

        assert "claude" in captured[0]
        assert "opus" in captured[0]

    async def test_no_args_shows_current(self, tmp_path):
        from tunapi.mattermost.commands import handle_model

        store = ChatPrefsStore(tmp_path / "prefs.json")
        captured: list[str] = []

        async def send(msg: RenderedMessage) -> None:
            captured.append(msg.text)

        await handle_model(
            "",
            channel_id="ch1",
            runtime=_FakeRuntime(),
            chat_prefs=store,
            send=send,
        )

        assert "Current engine" in captured[0]
        assert "Usage" in captured[0]
