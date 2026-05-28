from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from rich.console import Console
from rich.table import Table
from rich.text import Text

from tunapi.config import ConfigError
from tunapi.telegram import onboarding
from tunapi.telegram.api_models import Chat, Message, Update, User
from tunapi.telegram.client import TelegramRetryAfter
from unittest.mock import MagicMock, AsyncMock, patch

from tunapi.telegram.onboarding import (
    OnboardingCancelled,
    OnboardingState,
    build_transport_patch,
    build_config_patch,
    prompt_token,
    LiveServices,
    suppress_logging,
    display_path,
    ChatInfo,
    always_true,
)


class DummyUI:
    def __init__(
        self,
        *,
        confirms: list[bool | None] | None = None,
        selects: list[Any] | None = None,
        passwords: list[str | None] | None = None,
    ) -> None:
        self.confirms = iter(confirms or [])
        self.selects = iter(selects or [])
        self.passwords = iter(passwords or [])
        self.printed: list[object] = []
        self.steps: list[tuple[str, int]] = []

    def panel(
        self, title: str | None, body: str, *, border_style: str = "yellow"
    ) -> None:
        self.printed.append(("panel", title, body, border_style))

    def step(self, title: str, *, number: int) -> None:
        self.steps.append((title, number))

    def print(self, text: object = "", *, markup: bool | None = None) -> None:
        _ = markup
        self.printed.append(text)

    async def confirm(self, _prompt: str, default: bool = True) -> bool | None:
        _ = default
        return next(self.confirms)

    async def select(self, _prompt: str, choices: list[tuple[str, Any]]) -> Any | None:
        _ = choices
        return next(self.selects)

    async def password(self, _prompt: str) -> str | None:
        return next(self.passwords)


class DummyServices:
    def __init__(
        self,
        *,
        bot_info: list[User | None] | None = None,
        chat: onboarding.ChatInfo | None = None,
        topics_issue: ConfigError | None = None,
        engines: list[tuple[str, bool, str | None]] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.bot_info = iter(bot_info or [])
        self.chat = chat
        self.topics_issue = topics_issue
        self.engines = engines or []
        self.config = config or {}
        self.writes: list[tuple[Path, dict[str, Any]]] = []

    async def get_bot_info(self, _token: str) -> User | None:
        return next(self.bot_info)

    async def wait_for_chat(self, _token: str) -> onboarding.ChatInfo:
        assert self.chat is not None
        return self.chat

    async def validate_topics(
        self, _token: str, _chat_id: int, _scope: onboarding.TopicScope
    ) -> ConfigError | None:
        return self.topics_issue

    def list_engines(self) -> list[tuple[str, bool, str | None]]:
        return list(self.engines)

    def read_config(self, _path: Path) -> dict[str, Any]:
        return dict(self.config)

    def write_config(self, path: Path, data: dict[str, Any]) -> None:
        self.writes.append((path, data))


def test_chat_info_display_and_kind() -> None:
    chat = onboarding.ChatInfo(
        chat_id=1,
        username="alice",
        title=None,
        first_name=None,
        last_name=None,
        chat_type="private",
    )
    assert chat.display == "@alice"
    assert chat.kind == "private chat"

    group = onboarding.ChatInfo(
        chat_id=2,
        username=None,
        title="Team",
        first_name=None,
        last_name=None,
        chat_type="supergroup",
    )
    assert group.display == 'group "Team"'
    assert group.kind == 'supergroup "Team"'

    channel = onboarding.ChatInfo(
        chat_id=3,
        username=None,
        title=None,
        first_name=None,
        last_name=None,
        chat_type="channel",
    )
    assert channel.display == "channel"
    assert channel.kind == "channel"

    unnamed = onboarding.ChatInfo(
        chat_id=4,
        username=None,
        title=None,
        first_name="Ada",
        last_name="Lovelace",
        chat_type=None,
    )
    assert unnamed.display == "Ada Lovelace"
    assert unnamed.kind == "private chat"


def test_onboarding_state_helpers(tmp_path: Path) -> None:
    state = onboarding.OnboardingState(config_path=tmp_path / "cfg", force=False)
    assert state.is_stateful is False
    assert state.bot_ref == "your bot"

    state.session_mode = "chat"
    assert state.is_stateful is True

    state.session_mode = "stateless"
    state.topics_enabled = True
    assert state.is_stateful is True

    state.bot_name = "Tunapi"
    assert state.bot_ref == "Tunapi"
    state.bot_username = "tunapi_bot"
    assert state.bot_ref == "@tunapi_bot"


def test_display_path(tmp_path: Path) -> None:
    home_path = Path.home() / "tunapi" / "cfg.toml"
    assert onboarding.display_path(home_path).startswith("~/")
    assert onboarding.display_path(tmp_path / "cfg.toml") == str(tmp_path / "cfg.toml")


def test_build_transport_patch_requires_fields(tmp_path: Path) -> None:
    state = onboarding.OnboardingState(config_path=tmp_path / "cfg", force=False)
    with pytest.raises(RuntimeError, match="missing chat"):
        onboarding.build_transport_patch(state, bot_token="x")

    state.chat = onboarding.ChatInfo(
        chat_id=1,
        username=None,
        title=None,
        first_name=None,
        last_name=None,
        chat_type="private",
    )
    with pytest.raises(RuntimeError, match="missing session mode"):
        onboarding.build_transport_patch(state, bot_token="x")

    state.session_mode = "chat"
    with pytest.raises(RuntimeError, match="missing resume choice"):
        onboarding.build_transport_patch(state, bot_token="x")


def test_build_config_patch_and_merge(tmp_path: Path) -> None:
    state = onboarding.OnboardingState(config_path=tmp_path / "cfg", force=False)
    state.chat = onboarding.ChatInfo(
        chat_id=10,
        username=None,
        title=None,
        first_name=None,
        last_name=None,
        chat_type="private",
    )
    state.session_mode = "chat"
    state.show_resume_line = False
    state.default_engine = "codex"
    state.topics_enabled = True
    state.topics_scope = "all"

    patch = onboarding.build_config_patch(state, bot_token="token")
    assert patch["default_engine"] == "codex"

    merged = onboarding.merge_config(
        {"bot_token": "old", "chat_id": 1, "transports": {}},
        patch,
        config_path=tmp_path / "cfg.toml",
    )
    assert merged["transport"] == "telegram"
    assert merged["transports"]["telegram"]["bot_token"] == "token"
    assert merged["transports"]["telegram"]["topics"]["enabled"] is True
    assert "bot_token" not in merged
    assert "chat_id" not in merged


def test_render_helpers() -> None:
    text = onboarding.render_botfather_instructions()
    assert "BotFather" in text.plain
    assert "send /newbot" in text.plain

    topics = onboarding.render_topics_group_instructions("@bot")
    assert "topics" in topics.plain

    generic = onboarding.render_generic_capture_prompt("@bot")
    assert "send /start" in generic.plain

    warning = onboarding.render_topics_validation_warning(ConfigError("boom"))
    assert "warning" in warning.plain

    config_warning = onboarding.render_config_malformed_warning(ConfigError("bad"))
    assert "config is malformed" in config_warning.plain

    backup_warning = onboarding.render_backup_failed_warning(OSError("nope"))
    assert "failed to back up" in backup_warning.plain

    tabs = onboarding.render_persona_tabs()
    assert isinstance(tabs, Table)

    preview = onboarding.render_workspace_preview()
    assert "memory-box" in preview.plain

    assistant = onboarding.render_assistant_preview()
    assert "make happy wings fit" in assistant.plain

    handoff = onboarding.render_handoff_preview()
    assert "resume" in handoff.plain

    convo = Text()
    onboarding.append_dialogue(convo, "bot", "hello", speaker_style="cyan")
    assert "hello" in convo.plain


def test_render_engine_table_prints() -> None:
    ui = DummyUI()
    onboarding.render_engine_table(
        cast(onboarding.UI, ui),
        [("codex", True, None), ("other", False, "brew install other")],
    )
    assert ui.printed


def test_debug_onboarding_paths_prints_table() -> None:
    console = Console(record=True, width=120)
    onboarding.debug_onboarding_paths(console=console)
    output = console.export_text()
    assert "onboarding paths (15)" in output
    assert "workspace" in output
    assert "assistant" in output
    assert "handoff" in output


@pytest.mark.anyio
async def test_confirm_prompt_returns_question_result(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class _PromptSession:
        def __init__(self, *args, **kwargs) -> None:
            seen["args"] = args
            seen["kwargs"] = kwargs
            self.app = object()

    class _Question:
        def __init__(self, app) -> None:
            seen["app"] = app

        async def ask_async(self) -> bool | None:
            return True

    monkeypatch.setattr(onboarding, "PromptSession", _PromptSession)
    monkeypatch.setattr(onboarding, "Question", _Question)

    result = await onboarding.confirm_prompt("continue?", default=False)

    assert result is True
    assert "kwargs" in seen


@pytest.mark.anyio
async def test_get_bot_info_retries(monkeypatch) -> None:
    class _Bot:
        def __init__(self, _token: str) -> None:
            self.calls = 0
            self.closed = False

        async def get_me(self) -> User | None:
            self.calls += 1
            if self.calls < 3:
                raise TelegramRetryAfter(0)
            return User(id=1, username="bot")

        async def close(self) -> None:
            self.closed = True

    async def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(onboarding, "TelegramClient", _Bot)
    monkeypatch.setattr(onboarding.anyio, "sleep", _sleep)

    info = await onboarding.get_bot_info("token")
    assert info is not None
    assert info.username == "bot"


@pytest.mark.anyio
async def test_get_bot_info_gives_up(monkeypatch) -> None:
    class _Bot:
        def __init__(self, _token: str) -> None:
            self.calls = 0

        async def get_me(self) -> User | None:
            self.calls += 1
            raise TelegramRetryAfter(0)

        async def close(self) -> None:
            return None

    async def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(onboarding, "TelegramClient", _Bot)
    monkeypatch.setattr(onboarding.anyio, "sleep", _sleep)

    info = await onboarding.get_bot_info("token")
    assert info is None


@pytest.mark.anyio
async def test_wait_for_chat_filters_updates(monkeypatch) -> None:
    updates = [
        [
            Update(
                update_id=1,
                message=Message(
                    message_id=1,
                    from_=User(id=1, is_bot=True),
                    chat=Chat(id=1, type="private"),
                ),
            )
        ],
        None,
        [],
        [Update(update_id=2, message=None)],
        [
            Update(
                update_id=3,
                message=Message(
                    message_id=3,
                    from_=User(id=2, is_bot=True),
                    chat=Chat(id=2, type="private"),
                ),
            )
        ],
        [
            Update(
                update_id=4,
                message=Message(
                    message_id=4,
                    from_=User(id=3, is_bot=True),
                    chat=Chat(id=3, type="private"),
                ),
            )
        ],
        [
            Update(
                update_id=5,
                message=Message(
                    message_id=5,
                    from_=User(id=4, is_bot=True),
                    chat=Chat(id=4, type="private"),
                ),
            )
        ],
        [
            Update(
                update_id=6,
                message=Message(
                    message_id=6,
                    from_=User(id=5, is_bot=False),
                    chat=Chat(id=7, username="bob", type="private"),
                ),
            )
        ],
    ]

    class _Bot:
        def __init__(self, _token: str) -> None:
            self.calls = 0

        async def get_updates(self, *args, **kwargs):
            _ = args, kwargs
            idx = self.calls
            self.calls += 1
            return updates[idx]

        async def close(self) -> None:
            return None

    async def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(onboarding, "TelegramClient", _Bot)
    monkeypatch.setattr(onboarding.anyio, "sleep", _sleep)

    chat = await onboarding.wait_for_chat("token")
    assert chat.chat_id == 7
    assert chat.username == "bob"


@pytest.mark.anyio
async def test_validate_topics_onboarding_errors(monkeypatch) -> None:
    class _Bot:
        def __init__(self, _token: str) -> None:
            return None

        async def close(self) -> None:
            return None

    async def _raise_config(*_args, **_kwargs):
        raise ConfigError("bad")

    async def _raise_other(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(onboarding, "TelegramClient", _Bot)
    monkeypatch.setattr(onboarding, "_validate_topics_setup_for", _raise_config)
    err = await onboarding.validate_topics_onboarding("token", 1, "auto", ())
    assert isinstance(err, ConfigError)

    monkeypatch.setattr(onboarding, "_validate_topics_setup_for", _raise_other)
    err = await onboarding.validate_topics_onboarding("token", 1, "auto", ())
    assert isinstance(err, ConfigError)
    assert "topics validation failed" in str(err)


@pytest.mark.anyio
async def test_prompt_token_success_and_retry() -> None:
    ui = DummyUI(confirms=[True], passwords=["", "token", "token"])  # empty then retry
    svc = DummyServices(
        bot_info=[None, User(id=1, username="bot")],
    )

    token, info = await onboarding.prompt_token(
        cast(onboarding.UI, ui), cast(onboarding.Services, svc)
    )
    assert token == "token"
    assert info.username == "bot"


@pytest.mark.anyio
async def test_prompt_token_cancel_on_failure() -> None:
    ui = DummyUI(confirms=[False], passwords=["token"])
    svc = DummyServices(bot_info=[None])

    with pytest.raises(onboarding.OnboardingCancelled):
        await onboarding.prompt_token(
            cast(onboarding.UI, ui), cast(onboarding.Services, svc)
        )


@pytest.mark.anyio
async def test_capture_chat_sets_state() -> None:
    chat = onboarding.ChatInfo(
        chat_id=5,
        username=None,
        title="Team",
        first_name=None,
        last_name=None,
        chat_type="group",
    )
    ui = DummyUI()
    svc = DummyServices(chat=chat)
    state = onboarding.OnboardingState(config_path=Path("/tmp/cfg"), force=False)
    state.token = "token"

    await onboarding.capture_chat(
        cast(onboarding.UI, ui), cast(onboarding.Services, svc), state
    )
    assert state.chat == chat


@pytest.mark.anyio
async def test_step_capture_chat_workspace_switches_to_assistant(
    tmp_path: Path,
) -> None:
    chat = onboarding.ChatInfo(
        chat_id=6,
        username=None,
        title=None,
        first_name="Ada",
        last_name=None,
        chat_type="private",
    )
    ui = DummyUI(selects=["assistant"])
    svc = DummyServices(chat=chat, topics_issue=ConfigError("nope"))
    state = onboarding.OnboardingState(config_path=tmp_path / "cfg", force=False)
    state.token = "token"
    state.bot_name = "Tunapi"
    state.persona = "workspace"
    state.topics_scope = "auto"
    state.topics_enabled = True

    await onboarding.step_capture_chat(
        cast(onboarding.UI, ui), cast(onboarding.Services, svc), state
    )

    assert state.persona == "assistant"
    assert state.topics_enabled is False


@pytest.mark.anyio
async def test_step_default_engine_no_installed(tmp_path: Path) -> None:
    ui = DummyUI(confirms=[False])
    svc = DummyServices(engines=[("codex", False, None)])
    state = onboarding.OnboardingState(config_path=tmp_path / "cfg", force=False)

    with pytest.raises(onboarding.OnboardingCancelled):
        await onboarding.step_default_engine(
            cast(onboarding.UI, ui), cast(onboarding.Services, svc), state
        )


# ===========================================================================
# Migrated Onboarding Helper Tests
# ===========================================================================


class TestBuildTransportPatch:
    def test_valid(self):
        state = OnboardingState(config_path=Path("/cfg.toml"), force=False)
        state.chat = ChatInfo(
            chat_id=123,
            username="bot",
            title=None,
            first_name=None,
            last_name=None,
            chat_type="private",
        )
        state.session_mode = "chat"
        state.show_resume_line = False
        patch = build_transport_patch(state, bot_token="tok")
        assert patch["bot_token"] == "tok"
        assert patch["chat_id"] == 123
        assert patch["session_mode"] == "chat"
        assert patch["show_resume_line"] is False

    def test_missing_chat_raises(self):
        state = OnboardingState(config_path=Path("/cfg.toml"), force=False)
        state.session_mode = "chat"
        state.show_resume_line = False
        with pytest.raises(RuntimeError, match="missing chat"):
            build_transport_patch(state, bot_token="tok")

    def test_missing_session_mode_raises(self):
        state = OnboardingState(config_path=Path("/cfg.toml"), force=False)
        state.chat = ChatInfo(
            chat_id=1,
            username=None,
            title=None,
            first_name=None,
            last_name=None,
            chat_type=None,
        )
        state.show_resume_line = False
        with pytest.raises(RuntimeError, match="missing session mode"):
            build_transport_patch(state, bot_token="tok")

    def test_missing_resume_raises(self):
        state = OnboardingState(config_path=Path("/cfg.toml"), force=False)
        state.chat = ChatInfo(
            chat_id=1,
            username=None,
            title=None,
            first_name=None,
            last_name=None,
            chat_type=None,
        )
        state.session_mode = "chat"
        with pytest.raises(RuntimeError, match="missing resume"):
            build_transport_patch(state, bot_token="tok")


class TestBuildConfigPatch:
    def test_with_engine(self):
        state = OnboardingState(config_path=Path("/cfg.toml"), force=False)
        state.chat = ChatInfo(
            chat_id=1,
            username=None,
            title=None,
            first_name=None,
            last_name=None,
            chat_type=None,
        )
        state.session_mode = "chat"
        state.show_resume_line = True
        state.default_engine = "codex"
        patch = build_config_patch(state, bot_token="tok")
        assert patch["default_engine"] == "codex"
        assert patch["transport"] == "telegram"

    def test_without_engine(self):
        state = OnboardingState(config_path=Path("/cfg.toml"), force=False)
        state.chat = ChatInfo(
            chat_id=1,
            username=None,
            title=None,
            first_name=None,
            last_name=None,
            chat_type=None,
        )
        state.session_mode = "stateless"
        state.show_resume_line = True
        patch = build_config_patch(state, bot_token="tok")
        assert "default_engine" not in patch


class TestPromptToken:
    @pytest.mark.anyio
    async def test_success(self):
        ui = MagicMock()
        ui.password = AsyncMock(return_value="my-token")
        ui.print = MagicMock()
        user = User(id=1, is_bot=True, first_name="Bot", username="testbot")
        svc = MagicMock()
        svc.get_bot_info = AsyncMock(return_value=user)
        token, info = await prompt_token(ui, svc)
        assert token == "my-token"
        assert info.username == "testbot"

    @pytest.mark.anyio
    async def test_empty_then_success(self):
        ui = MagicMock()
        ui.password = AsyncMock(side_effect=["", "real-token"])
        ui.print = MagicMock()
        user = User(id=1, is_bot=True, first_name="Bot", username=None)
        svc = MagicMock()
        svc.get_bot_info = AsyncMock(return_value=user)
        token, info = await prompt_token(ui, svc)
        assert token == "real-token"

    @pytest.mark.anyio
    async def test_failed_retry_cancel(self):
        ui = MagicMock()
        ui.password = AsyncMock(return_value="bad-token")
        ui.confirm = AsyncMock(return_value=False)
        ui.print = MagicMock()
        svc = MagicMock()
        svc.get_bot_info = AsyncMock(return_value=None)
        with pytest.raises(OnboardingCancelled):
            await prompt_token(ui, svc)

    @pytest.mark.anyio
    async def test_password_returns_none_raises(self):
        ui = MagicMock()
        ui.password = AsyncMock(return_value=None)
        ui.print = MagicMock()
        svc = MagicMock()
        with pytest.raises(OnboardingCancelled):
            await prompt_token(ui, svc)


class TestLiveServices:
    def test_list_engines(self):
        svc = LiveServices()
        with patch("tunapi.telegram.onboarding.list_backends") as mock_lb:
            be = MagicMock()
            be.id = "claude"
            be.cli_cmd = "claude"
            be.install_cmd = None
            mock_lb.return_value = [be]
            with patch("shutil.which", return_value="/usr/bin/claude"):
                rows = svc.list_engines()
        assert len(rows) == 1
        assert rows[0] == ("claude", True, None)

    def test_read_config(self, tmp_path: Path):
        svc = LiveServices()
        cfg = tmp_path / "tunapi.toml"
        cfg.write_text('[transports]\n[transports.telegram]\nbot_token = "tok"\n')
        data = svc.read_config(cfg)
        assert "transports" in data

    def test_write_config(self, tmp_path: Path):
        svc = LiveServices()
        cfg = tmp_path / "tunapi.toml"
        svc.write_config(cfg, {"transport": "telegram"})
        assert cfg.exists()


class TestStepCaptureChat:
    @pytest.mark.anyio
    async def test_missing_persona_raises(self):
        ui = MagicMock()
        svc = MagicMock()
        state = OnboardingState(config_path=Path("/x"), force=False)
        with pytest.raises(RuntimeError, match="missing persona"):
            from tunapi.telegram.onboarding import step_capture_chat

            await step_capture_chat(ui, svc, state)

    @pytest.mark.anyio
    async def test_assistant_mode(self):
        ui = MagicMock()
        ui.print = MagicMock()
        chat = ChatInfo(
            chat_id=1,
            username="u",
            title=None,
            first_name=None,
            last_name=None,
            chat_type="private",
        )
        svc = MagicMock()
        svc.wait_for_chat = AsyncMock(return_value=chat)
        state = OnboardingState(config_path=Path("/x"), force=False)
        state.token = "tok"
        state.persona = "assistant"
        from tunapi.telegram.onboarding import step_capture_chat

        await step_capture_chat(ui, svc, state)
        assert state.chat is not None

    @pytest.mark.anyio
    async def test_workspace_success(self):
        ui = MagicMock()
        ui.print = MagicMock()
        chat = ChatInfo(
            chat_id=-100,
            username=None,
            title="Team",
            first_name=None,
            last_name=None,
            chat_type="supergroup",
        )
        svc = MagicMock()
        svc.wait_for_chat = AsyncMock(return_value=chat)
        svc.validate_topics = AsyncMock(return_value=None)
        state = OnboardingState(config_path=Path("/x"), force=False)
        state.token = "tok"
        state.persona = "workspace"
        from tunapi.telegram.onboarding import step_capture_chat

        await step_capture_chat(ui, svc, state)
        assert state.chat is not None

    @pytest.mark.anyio
    async def test_workspace_validation_fails_switch_to_assistant(self):
        from tunapi.config import ConfigError

        ui = MagicMock()
        ui.print = MagicMock()
        ui.select = AsyncMock(return_value="assistant")
        chat = ChatInfo(
            chat_id=-100,
            username=None,
            title="Team",
            first_name=None,
            last_name=None,
            chat_type="supergroup",
        )
        svc = MagicMock()
        svc.wait_for_chat = AsyncMock(return_value=chat)
        svc.validate_topics = AsyncMock(return_value=ConfigError("no topics"))
        state = OnboardingState(config_path=Path("/x"), force=False)
        state.token = "tok"
        state.persona = "workspace"
        from tunapi.telegram.onboarding import step_capture_chat

        await step_capture_chat(ui, svc, state)
        assert state.persona == "assistant"
        assert state.topics_enabled is False

    @pytest.mark.anyio
    async def test_workspace_validation_retry_then_ok(self):
        from tunapi.config import ConfigError

        ui = MagicMock()
        ui.print = MagicMock()
        # select is only called once (on first failure), then retry succeeds
        ui.select = AsyncMock(side_effect=["retry"])
        svc = MagicMock()
        chat = ChatInfo(
            chat_id=-100,
            username=None,
            title="Team",
            first_name=None,
            last_name=None,
            chat_type="supergroup",
        )
        svc.wait_for_chat = AsyncMock(return_value=chat)
        # First validation fails, retry succeeds
        call_count = 0

        async def validate_side_effect(token, chat_id, scope):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ConfigError("no topics")
            return None

        svc.validate_topics = validate_side_effect
        state = OnboardingState(config_path=Path("/x"), force=False)
        state.token = "tok"
        state.persona = "workspace"
        from tunapi.telegram.onboarding import step_capture_chat

        await step_capture_chat(ui, svc, state)
        assert state.persona == "workspace"

    @pytest.mark.anyio
    async def test_workspace_validation_cancel(self):
        from tunapi.config import ConfigError

        ui = MagicMock()
        ui.print = MagicMock()
        ui.select = AsyncMock(return_value=None)
        svc = MagicMock()
        chat = ChatInfo(
            chat_id=-100,
            username=None,
            title="Team",
            first_name=None,
            last_name=None,
            chat_type="supergroup",
        )
        svc.wait_for_chat = AsyncMock(return_value=chat)
        svc.validate_topics = AsyncMock(return_value=ConfigError("no topics"))
        state = OnboardingState(config_path=Path("/x"), force=False)
        state.token = "tok"
        state.persona = "workspace"
        with pytest.raises(OnboardingCancelled):
            from tunapi.telegram.onboarding import step_capture_chat

            await step_capture_chat(ui, svc, state)


class TestSuppressLogging:
    def test_context_manager(self):
        with suppress_logging():
            pass  # should not raise


class TestDisplayPath:
    def test_non_home(self):
        result = display_path(Path("/etc/config.toml"))
        assert result == "/etc/config.toml"


class TestAlwaysTrue:
    def test_returns_true(self):
        state = OnboardingState(config_path=Path("/x"), force=False)
        assert always_true(state) is True
