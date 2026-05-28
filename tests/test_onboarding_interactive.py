# ruff: noqa: E402
from __future__ import annotations

import anyio
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from tunapi.backends import EngineBackend
from tunapi.config import dump_toml
from tunapi.telegram import onboarding
from tunapi.telegram.api_models import User

pytestmark = pytest.mark.anyio


def test_mask_token_short() -> None:
    assert onboarding.mask_token("short") == "*****"


def test_mask_token_long() -> None:
    token = "123456789:ABCdefGH"
    masked = onboarding.mask_token(token)
    assert masked.startswith("123456789")
    assert masked.endswith("defGH")
    assert "..." in masked


def test_render_config_escapes() -> None:
    config = dump_toml(
        {
            "default_engine": "codex",
            "transport": "telegram",
            "transports": {
                "telegram": {
                    "bot_token": 'token"with\\quote',
                    "chat_id": 123,
                }
            },
        }
    )
    assert 'default_engine = "codex"' in config
    assert 'transport = "telegram"' in config
    assert "[transports.telegram]" in config
    assert 'bot_token = "token\\"with\\\\quote"' in config
    assert "chat_id = 123" in config
    assert config.endswith("\n")


class FakeQuestion:
    def __init__(self, value):
        self._value = value

    def ask(self):
        return self._value

    async def ask_async(self):
        return self._value


def queue_answers(values):
    it = iter(values)

    def _make(*_args, **_kwargs):
        return FakeQuestion(next(it))

    return _make


def queue_values(values):
    it = iter(values)

    async def _next(*_args, **_kwargs):
        return next(it)

    return _next


def patch_live_services(
    monkeypatch,
    *,
    bot: User,
    chat: onboarding.ChatInfo,
    topics_issue=None,
) -> None:
    async def _get_bot_info(self, _token: str):
        return bot

    async def _wait_for_chat(self, _token: str):
        return chat

    async def _validate_topics(self, _token: str, _chat_id: int, _scope):
        return topics_issue

    monkeypatch.setattr(onboarding.LiveServices, "get_bot_info", _get_bot_info)
    monkeypatch.setattr(onboarding.LiveServices, "wait_for_chat", _wait_for_chat)
    monkeypatch.setattr(onboarding.LiveServices, "validate_topics", _validate_topics)


def test_interactive_setup_skips_when_config_exists(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "tunapi.toml"
    config_path.write_text(
        'transport = "telegram"\n\n[transports.telegram]\n'
        'bot_token = "token"\nchat_id = 123\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(onboarding, "HOME_CONFIG_PATH", config_path)
    assert anyio.run(partial(onboarding.interactive_setup, force=False)) is True


def test_interactive_setup_writes_config(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "tunapi.toml"
    monkeypatch.setattr(onboarding, "HOME_CONFIG_PATH", config_path)

    backend = EngineBackend(id="codex", build_runner=lambda _cfg, _path: None)
    monkeypatch.setattr(onboarding, "list_backends", lambda: [backend])
    monkeypatch.setattr(onboarding.shutil, "which", lambda _cmd: "/usr/bin/codex")

    monkeypatch.setattr(onboarding, "confirm_prompt", queue_values([True, True]))
    monkeypatch.setattr(
        onboarding.questionary, "password", queue_answers(["123456789:ABCdef"])
    )
    monkeypatch.setattr(
        onboarding.questionary,
        "select",
        queue_answers(["assistant", "codex"]),
    )
    patch_live_services(
        monkeypatch,
        bot=User(id=1, username="my_bot"),
        chat=onboarding.ChatInfo(
            chat_id=123,
            username="alice",
            title=None,
            first_name="Alice",
            last_name=None,
            chat_type="private",
        ),
    )

    assert anyio.run(partial(onboarding.interactive_setup, force=False)) is True
    saved = config_path.read_text(encoding="utf-8")
    assert 'transport = "telegram"' in saved
    assert "[transports.telegram]" in saved
    assert 'bot_token = "123456789:ABCdef"' in saved
    assert "chat_id = 123" in saved
    assert 'session_mode = "chat"' in saved
    assert "show_resume_line = false" in saved
    assert "[transports.telegram.topics]" in saved
    assert "enabled = false" in saved
    assert 'default_engine = "codex"' in saved


def test_interactive_setup_preserves_projects(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "tunapi.toml"
    config_path.write_text(
        'default_project = "z80"\n\n[projects.z80]\npath = "/tmp/repo"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(onboarding, "HOME_CONFIG_PATH", config_path)

    backend = EngineBackend(id="codex", build_runner=lambda _cfg, _path: None)
    monkeypatch.setattr(onboarding, "list_backends", lambda: [backend])
    monkeypatch.setattr(onboarding.shutil, "which", lambda _cmd: "/usr/bin/codex")

    monkeypatch.setattr(onboarding, "confirm_prompt", queue_values([True, True, True]))
    monkeypatch.setattr(
        onboarding.questionary, "password", queue_answers(["123456789:ABCdef"])
    )
    monkeypatch.setattr(
        onboarding.questionary,
        "select",
        queue_answers(["assistant", "codex"]),
    )
    patch_live_services(
        monkeypatch,
        bot=User(id=1, username="my_bot"),
        chat=onboarding.ChatInfo(
            chat_id=123,
            username="alice",
            title=None,
            first_name="Alice",
            last_name=None,
            chat_type="private",
        ),
    )

    assert anyio.run(partial(onboarding.interactive_setup, force=True)) is True
    saved = config_path.read_text(encoding="utf-8")
    assert "[projects.z80]" in saved
    assert 'path = "/tmp/repo"' in saved


def test_interactive_setup_no_agents_aborts(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "tunapi.toml"
    monkeypatch.setattr(onboarding, "HOME_CONFIG_PATH", config_path)

    backend = EngineBackend(id="codex", build_runner=lambda _cfg, _path: None)
    monkeypatch.setattr(onboarding, "list_backends", lambda: [backend])
    monkeypatch.setattr(onboarding.shutil, "which", lambda _cmd: None)

    monkeypatch.setattr(onboarding, "confirm_prompt", queue_values([True, False]))
    monkeypatch.setattr(
        onboarding.questionary, "password", queue_answers(["123456789:ABCdef"])
    )
    monkeypatch.setattr(
        onboarding.questionary,
        "select",
        queue_answers(["assistant"]),
    )
    patch_live_services(
        monkeypatch,
        bot=User(id=1, username="my_bot"),
        chat=onboarding.ChatInfo(
            chat_id=123,
            username="alice",
            title=None,
            first_name="Alice",
            last_name=None,
            chat_type="private",
        ),
    )

    assert anyio.run(partial(onboarding.interactive_setup, force=False)) is False
    assert not config_path.exists()


def test_interactive_setup_recovers_from_malformed_toml(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "tunapi.toml"
    bad_toml = 'transport = "telegram"\n[transports\n'
    config_path.write_text(bad_toml, encoding="utf-8")
    monkeypatch.setattr(onboarding, "HOME_CONFIG_PATH", config_path)

    backend = EngineBackend(id="codex", build_runner=lambda _cfg, _path: None)
    monkeypatch.setattr(onboarding, "list_backends", lambda: [backend])
    monkeypatch.setattr(onboarding.shutil, "which", lambda _cmd: "/usr/bin/codex")

    monkeypatch.setattr(onboarding, "confirm_prompt", queue_values([True, True, True]))
    monkeypatch.setattr(
        onboarding.questionary, "password", queue_answers(["123456789:ABCdef"])
    )
    monkeypatch.setattr(
        onboarding.questionary,
        "select",
        queue_answers(["assistant", "codex"]),
    )
    patch_live_services(
        monkeypatch,
        bot=User(id=1, username="my_bot"),
        chat=onboarding.ChatInfo(
            chat_id=123,
            username="alice",
            title=None,
            first_name="Alice",
            last_name=None,
            chat_type="private",
        ),
    )

    assert anyio.run(partial(onboarding.interactive_setup, force=True)) is True
    backup = config_path.with_suffix(".toml.bak")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == bad_toml
    saved = config_path.read_text(encoding="utf-8")
    assert "[transports.telegram]" in saved
    assert 'bot_token = "123456789:ABCdef"' in saved


def test_capture_chat_id_with_token(monkeypatch) -> None:
    patch_live_services(
        monkeypatch,
        bot=User(id=1, username="my_bot"),
        chat=onboarding.ChatInfo(
            chat_id=456,
            username=None,
            title="tunapi",
            first_name=None,
            last_name=None,
            chat_type="supergroup",
        ),
    )

    chat = anyio.run(partial(onboarding.capture_chat_id, token="123456789:ABCdef"))

    assert chat is not None
    assert chat.chat_id == 456


def test_capture_chat_id_prompts_for_token(monkeypatch) -> None:
    async def _prompt_token(_ui, _svc):
        return ("token", User(id=1, username="bot"))

    monkeypatch.setattr(onboarding, "prompt_token", _prompt_token)
    patch_live_services(
        monkeypatch,
        bot=User(id=1, username="bot"),
        chat=onboarding.ChatInfo(
            chat_id=789,
            username="alice",
            title=None,
            first_name="Alice",
            last_name=None,
            chat_type="private",
        ),
    )

    chat = anyio.run(onboarding.capture_chat_id)

    assert chat is not None
    assert chat.chat_id == 789


# test_coverage_push.py에서 가져온 Onboarding Interactive 테스트들
from tunapi.telegram.onboarding import (
    OnboardingCancelled,
    OnboardingState,
    OnboardingStep,
    step_persona,
    run_onboarding,
    step_default_engine,
    capture_chat,
    step_save_config,
    step_token_and_bot,
    ChatInfo,
)


class TestStepPersona:
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


class TestRunOnboarding:
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


class TestStepDefaultEngine:
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


class TestCaptureChat:
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


class TestStepTokenAndBot:
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


class TestStepSaveConfig:
    async def test_save_declined(self):
        ui = MagicMock()
        ui.confirm = AsyncMock(return_value=False)
        ui.svc = MagicMock()
        state = OnboardingState(config_path=Path("/x"), force=False)
        with pytest.raises(OnboardingCancelled):
            await step_save_config(ui, ui.svc, state)

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
