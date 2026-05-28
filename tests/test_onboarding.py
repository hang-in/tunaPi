from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tunapi import engines
from tunapi.settings import TunapiSettings
from tunapi.telegram import onboarding
from tunapi.telegram.api_models import User
from tunapi.telegram.onboarding import (
    ChatInfo,
    OnboardingCancelled,
    OnboardingState,
    OnboardingStep,
    capture_chat,
    run_onboarding,
    step_default_engine,
    step_persona,
    step_save_config,
    step_token_and_bot,
)

pytestmark = pytest.mark.anyio


def test_check_setup_marks_missing_codex(monkeypatch, tmp_path: Path) -> None:
    backend = engines.get_backend("codex")
    monkeypatch.setattr(onboarding.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        onboarding,
        "load_settings",
        lambda: (
            TunapiSettings.model_validate(
                {
                    "transport": "telegram",
                    "transports": {"telegram": {"bot_token": "token", "chat_id": 123}},
                }
            ),
            tmp_path / "tunapi.toml",
        ),
    )

    result = onboarding.check_setup(backend)

    titles = {issue.title for issue in result.issues}
    assert "install codex" in titles
    assert "create a config" not in titles
    assert result.ok is False


def test_check_setup_marks_missing_config(monkeypatch, tmp_path: Path) -> None:
    backend = engines.get_backend("codex")
    monkeypatch.setattr(onboarding.shutil, "which", lambda _name: "/usr/bin/codex")
    monkeypatch.setattr(onboarding, "HOME_CONFIG_PATH", tmp_path / "tunapi.toml")

    def _raise() -> None:
        raise onboarding.ConfigError("Missing config file")

    monkeypatch.setattr(onboarding, "load_settings", _raise)

    result = onboarding.check_setup(backend)

    titles = {issue.title for issue in result.issues}
    assert "create a config" in titles
    assert result.config_path == onboarding.HOME_CONFIG_PATH


def test_check_setup_marks_invalid_bot_token(monkeypatch, tmp_path: Path) -> None:
    backend = engines.get_backend("codex")
    monkeypatch.setattr(onboarding.shutil, "which", lambda _name: "/usr/bin/codex")

    def _fail_require(*_args, **_kwargs):
        raise onboarding.ConfigError("Missing bot token")

    monkeypatch.setattr(
        onboarding,
        "load_settings",
        lambda: (
            TunapiSettings.model_validate(
                {
                    "transport": "telegram",
                    "transports": {"telegram": {"bot_token": "token", "chat_id": 123}},
                }
            ),
            tmp_path / "tunapi.toml",
        ),
    )
    monkeypatch.setattr(onboarding, "require_telegram", _fail_require)

    result = onboarding.check_setup(backend)

    titles = {issue.title for issue in result.issues}
    assert "configure telegram" in titles


def test_check_setup_skips_telegram_validation_for_external_transport(
    monkeypatch, tmp_path: Path
) -> None:
    backend = engines.get_backend("codex")
    monkeypatch.setattr(onboarding.shutil, "which", lambda _name: "/usr/bin/codex")
    monkeypatch.setattr(
        onboarding,
        "load_settings",
        lambda: (
            TunapiSettings.model_validate(
                {"transport": "my-transport", "transports": {}}
            ),
            tmp_path / "tunapi.toml",
        ),
    )

    result = onboarding.check_setup(backend, transport_override="my-transport")

    assert result.ok is True
    assert len(result.issues) == 0


def test_check_setup_external_transport_missing_config(
    monkeypatch, tmp_path: Path
) -> None:
    backend = engines.get_backend("codex")
    monkeypatch.setattr(onboarding.shutil, "which", lambda _name: "/usr/bin/codex")
    monkeypatch.setattr(onboarding, "HOME_CONFIG_PATH", tmp_path / "tunapi.toml")

    def _raise() -> None:
        raise onboarding.ConfigError("Missing config file")

    monkeypatch.setattr(onboarding, "load_settings", _raise)

    result = onboarding.check_setup(backend, transport_override="my-transport")

    titles = {issue.title for issue in result.issues}
    assert "create a config" in titles
    assert "configure telegram" not in titles


class TestStepPersonaPush:
    async def test_workspace(self):
        ui = MagicMock()
        ui.select = AsyncMock(return_value="workspace")
        ui.print = MagicMock()
        svc = MagicMock()
        state = OnboardingState(config_path=Path("/x"), force=False)
        await step_persona(ui, svc, state)
        assert state.persona == "workspace"
        assert state.session_mode == "chat"
        assert state.topics_enabled is True

    async def test_assistant(self):
        ui = MagicMock()
        ui.select = AsyncMock(return_value="assistant")
        ui.print = MagicMock()
        svc = MagicMock()
        state = OnboardingState(config_path=Path("/x"), force=False)
        await step_persona(ui, svc, state)
        assert state.persona == "assistant"
        assert state.session_mode == "chat"
        assert state.topics_enabled is False

    async def test_handoff(self):
        ui = MagicMock()
        ui.select = AsyncMock(return_value="handoff")
        ui.print = MagicMock()
        svc = MagicMock()
        state = OnboardingState(config_path=Path("/x"), force=False)
        await step_persona(ui, svc, state)
        assert state.persona == "handoff"
        assert state.session_mode == "stateless"
        assert state.show_resume_line is True


class TestRunOnboardingPush:
    async def test_cancelled(self):
        ui = MagicMock()
        svc = MagicMock()
        state = OnboardingState(config_path=Path("/x"), force=False)
        failing_step = OnboardingStep(
            title="fail",
            number=1,
            run=AsyncMock(side_effect=OnboardingCancelled()),
        )
        with patch("tunapi.telegram.onboarding.STEPS", [failing_step]):
            result = await run_onboarding(ui, svc, state)
        assert result is False

    async def test_skip_non_applicable(self):
        ui = MagicMock()
        svc = MagicMock()
        state = OnboardingState(config_path=Path("/x"), force=False)
        skipped = OnboardingStep(
            title="skip",
            number=1,
            run=AsyncMock(),
            applies=lambda _: False,
        )
        with patch("tunapi.telegram.onboarding.STEPS", [skipped]):
            result = await run_onboarding(ui, svc, state)
        assert result is True
        skipped.run.assert_not_called()


class TestStepDefaultEnginePush:
    async def test_installed_engines(self):
        ui = MagicMock()
        ui.select = AsyncMock(return_value="claude")
        ui.print = MagicMock()
        svc = MagicMock()
        svc.list_engines.return_value = [
            ("claude", True, None),
            ("codex", False, "npm i codex"),
        ]
        state = OnboardingState(config_path=Path("/x"), force=False)
        await step_default_engine(ui, svc, state)
        assert state.default_engine == "claude"

    async def test_no_engines_save_anyway(self):
        ui = MagicMock()
        ui.confirm = AsyncMock(return_value=True)
        ui.print = MagicMock()
        svc = MagicMock()
        svc.list_engines.return_value = [("claude", False, "install")]
        state = OnboardingState(config_path=Path("/x"), force=False)
        await step_default_engine(ui, svc, state)
        assert state.default_engine is None

    async def test_no_engines_cancel(self):
        ui = MagicMock()
        ui.confirm = AsyncMock(return_value=False)
        ui.print = MagicMock()
        svc = MagicMock()
        svc.list_engines.return_value = []
        state = OnboardingState(config_path=Path("/x"), force=False)
        with pytest.raises(OnboardingCancelled):
            await step_default_engine(ui, svc, state)


class TestCaptureChatPush:
    async def test_missing_token(self):
        ui = MagicMock()
        svc = MagicMock()
        state = OnboardingState(config_path=Path("/x"), force=False)
        with pytest.raises(RuntimeError, match="missing token"):
            await capture_chat(ui, svc, state)

    async def test_success(self):
        ui = MagicMock()
        ui.print = MagicMock()
        chat_info = ChatInfo(
            chat_id=99,
            username="me",
            title=None,
            first_name="Alice",
            last_name=None,
            chat_type="private",
        )
        svc = MagicMock()
        svc.wait_for_chat = AsyncMock(return_value=chat_info)
        state = OnboardingState(config_path=Path("/x"), force=False)
        state.token = "tok"
        await capture_chat(ui, svc, state)
        assert state.chat is not None
        assert state.chat.chat_id == 99

    async def test_group_chat(self):
        ui = MagicMock()
        ui.print = MagicMock()
        chat_info = ChatInfo(
            chat_id=-100,
            username=None,
            title="Dev Team",
            first_name=None,
            last_name=None,
            chat_type="supergroup",
        )
        svc = MagicMock()
        svc.wait_for_chat = AsyncMock(return_value=chat_info)
        state = OnboardingState(config_path=Path("/x"), force=False)
        state.token = "tok"
        await capture_chat(ui, svc, state)
        assert state.chat.chat_type == "supergroup"


class TestStepSaveConfigPush:
    async def test_save_declined(self):
        ui = MagicMock()
        ui.confirm = AsyncMock(return_value=False)
        svc = MagicMock()
        state = OnboardingState(config_path=Path("/x"), force=False)
        with pytest.raises(OnboardingCancelled):
            await step_save_config(ui, svc, state)

    async def test_save_ok(self, tmp_path: Path):
        config_path = tmp_path / "tunapi.toml"
        ui = MagicMock()
        ui.confirm = AsyncMock(return_value=True)
        ui.print = MagicMock()
        svc = MagicMock()
        svc.write_config = MagicMock()
        state = OnboardingState(config_path=config_path, force=False)
        state.token = "tok"
        state.chat = ChatInfo(
            chat_id=1,
            username=None,
            title=None,
            first_name=None,
            last_name=None,
            chat_type=None,
        )
        state.session_mode = "chat"
        state.show_resume_line = False
        await step_save_config(ui, svc, state)
        svc.write_config.assert_called_once()

    async def test_save_malformed_existing(self, tmp_path: Path):
        from tunapi.config import ConfigError

        config_path = tmp_path / "tunapi.toml"
        config_path.write_text("bad toml {{")
        ui = MagicMock()
        ui.confirm = AsyncMock(return_value=True)
        ui.print = MagicMock()
        svc = MagicMock()
        svc.read_config = MagicMock(side_effect=ConfigError("bad"))
        svc.write_config = MagicMock()
        state = OnboardingState(config_path=config_path, force=False)
        state.token = "tok"
        state.chat = ChatInfo(
            chat_id=1,
            username=None,
            title=None,
            first_name=None,
            last_name=None,
            chat_type=None,
        )
        state.session_mode = "chat"
        state.show_resume_line = False
        await step_save_config(ui, svc, state)
        # Should still write config despite malformed existing
        svc.write_config.assert_called_once()
        # Backup should have been attempted
        assert (config_path.with_suffix(".toml.bak")).exists()


class TestStepTokenAndBotPush:
    async def test_have_token(self):
        ui = MagicMock()
        ui.confirm = AsyncMock(return_value=True)
        ui.password = AsyncMock(return_value="tok123")
        ui.print = MagicMock()
        user = User(id=1, is_bot=True, first_name="Bot", username="mybot")
        svc = MagicMock()
        svc.get_bot_info = AsyncMock(return_value=user)
        state = OnboardingState(config_path=Path("/x"), force=False)
        await step_token_and_bot(ui, svc, state)
        assert state.token == "tok123"
        assert state.bot_username == "mybot"
        assert state.bot_name == "Bot"

    async def test_no_token_shows_instructions(self):
        ui = MagicMock()
        ui.confirm = AsyncMock(return_value=False)
        ui.password = AsyncMock(return_value="tok123")
        ui.print = MagicMock()
        user = User(id=1, is_bot=True, first_name="Bot", username=None)
        svc = MagicMock()
        svc.get_bot_info = AsyncMock(return_value=user)
        state = OnboardingState(config_path=Path("/x"), force=False)
        await step_token_and_bot(ui, svc, state)
        assert state.token == "tok123"
        # Should have printed botfather instructions
        assert ui.print.call_count >= 2
