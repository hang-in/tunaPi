"""Tests for tunapi.discord.backend module."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tunapi.config import ProjectConfig, ProjectsConfig
from tunapi.discord import backend as discord_backend
from tunapi.discord.backend import (
    DiscordBackend,
    _build_startup_message,
    _get_discord_settings,
)
from tunapi.discord.bridge import DiscordBridgeConfig
from tunapi.router import AutoRouter, RunnerEntry
from tunapi.runners.mock import Return, ScriptRunner
from tunapi.transport_runtime import TransportRuntime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runtime(
    *,
    engines: list[tuple[str, str | None, str | None]] | None = None,
    default_engine: str = "codex",
    projects: dict | None = None,
) -> TransportRuntime:
    """Build a TransportRuntime with the given engine entries.

    Each entry in *engines* is (engine_id, status, issue).
    status=None means available.
    """
    if engines is None:
        engines = [(default_engine, None, None)]

    entries = []
    for engine_id, status, issue in engines:
        runner = ScriptRunner([Return(answer="ok")], engine=engine_id)
        entry_kw: dict[str, Any] = {"engine": engine_id, "runner": runner}
        if status is not None:
            entry_kw["status"] = status
            entry_kw["issue"] = issue or status
        entries.append(RunnerEntry(**entry_kw))

    router = AutoRouter(entries=entries, default_engine=default_engine)
    return TransportRuntime(
        router=router,
        projects=ProjectsConfig(projects=projects or {}, default_project=None),
        watch_config=True,
    )


# ---------------------------------------------------------------------------
# _get_discord_settings
# ---------------------------------------------------------------------------


class TestGetDiscordSettings:
    def test_dict_passthrough(self) -> None:
        d = {"bot_token": "tok", "guild_id": 42}
        assert _get_discord_settings(d) is d

    def test_pydantic_model(self) -> None:
        model = MagicMock()
        model.model_dump.return_value = {"bot_token": "tok"}
        result = _get_discord_settings(model)
        assert result == {"bot_token": "tok"}
        model.model_dump.assert_called_once()

    def test_invalid_type_raises(self) -> None:
        with pytest.raises(TypeError, match="unexpected transport_config type"):
            _get_discord_settings(42)

    def test_invalid_string_raises(self) -> None:
        with pytest.raises(TypeError, match="unexpected transport_config type"):
            _get_discord_settings("not a dict")

    def test_object_without_model_dump_raises(self) -> None:
        obj = object()
        with pytest.raises(TypeError):
            _get_discord_settings(obj)


# ---------------------------------------------------------------------------
# _build_startup_message
# ---------------------------------------------------------------------------


class TestBuildStartupMessage:
    def test_basic_single_engine(self, tmp_path: Path) -> None:
        runtime = _make_runtime(engines=[("codex", None, None)])
        msg = _build_startup_message(runtime, startup_pwd=str(tmp_path))

        assert "tunapi-discord is ready" in msg
        assert "default: `codex`" in msg
        assert "agents: `codex`" in msg
        assert "projects: `none`" in msg
        assert str(tmp_path) in msg

    def test_multiple_available_engines(self, tmp_path: Path) -> None:
        runtime = _make_runtime(
            engines=[("codex", None, None), ("claude", None, None)],
            default_engine="codex",
        )
        msg = _build_startup_message(runtime, startup_pwd=str(tmp_path))
        assert "codex" in msg
        assert "claude" in msg

    def test_missing_engines_noted(self, tmp_path: Path) -> None:
        runtime = _make_runtime(
            engines=[
                ("codex", None, None),
                ("pi", "missing_cli", "not found"),
            ],
        )
        msg = _build_startup_message(runtime, startup_pwd=str(tmp_path))
        assert "not installed: pi" in msg

    def test_misconfigured_engines_noted(self, tmp_path: Path) -> None:
        runtime = _make_runtime(
            engines=[
                ("codex", None, None),
                ("claude", "bad_config", "bad key"),
            ],
        )
        msg = _build_startup_message(runtime, startup_pwd=str(tmp_path))
        assert "misconfigured: claude" in msg

    def test_failed_engines_noted(self, tmp_path: Path) -> None:
        runtime = _make_runtime(
            engines=[
                ("codex", None, None),
                ("gemini", "load_error", "crash"),
            ],
        )
        msg = _build_startup_message(runtime, startup_pwd=str(tmp_path))
        assert "failed to load: gemini" in msg

    def test_all_unavailable_reasons_combined(self, tmp_path: Path) -> None:
        runtime = _make_runtime(
            engines=[
                ("codex", None, None),
                ("pi", "missing_cli", "missing"),
                ("claude", "bad_config", "bad"),
                ("gemini", "load_error", "err"),
            ],
        )
        msg = _build_startup_message(runtime, startup_pwd=str(tmp_path))
        assert "not installed: pi" in msg
        assert "misconfigured: claude" in msg
        assert "failed to load: gemini" in msg

    def test_no_available_engines(self, tmp_path: Path) -> None:
        runtime = _make_runtime(
            engines=[("codex", "missing_cli", "missing")],
            default_engine="codex",
        )
        msg = _build_startup_message(runtime, startup_pwd=str(tmp_path))
        assert "agents: `none" in msg

    def test_projects_listed(self, tmp_path: Path) -> None:
        proj = ProjectConfig(
            alias="myproj",
            path=Path("/tmp/myproj"),
            worktrees_dir=Path(".worktrees"),
        )
        runtime = _make_runtime(projects={"myproj": proj})
        msg = _build_startup_message(runtime, startup_pwd=str(tmp_path))
        assert "myproj" in msg


# ---------------------------------------------------------------------------
# DiscordBackend.lock_token
# ---------------------------------------------------------------------------


class TestLockToken:
    def test_extracts_bot_token_from_dict(self) -> None:
        backend = DiscordBackend()
        token = backend.lock_token(
            transport_config={"bot_token": "secret-token", "guild_id": 1},
            _config_path=Path("/tmp/tunapi.toml"),
        )
        assert token == "secret-token"

    def test_returns_none_when_no_token(self) -> None:
        backend = DiscordBackend()
        token = backend.lock_token(
            transport_config={"guild_id": 1},
            _config_path=Path("/tmp/tunapi.toml"),
        )
        assert token is None

    def test_extracts_from_pydantic_model(self) -> None:
        model = MagicMock()
        model.model_dump.return_value = {"bot_token": "pydantic-tok"}
        backend = DiscordBackend()
        token = backend.lock_token(
            transport_config=model,
            _config_path=Path("/tmp/tunapi.toml"),
        )
        assert token == "pydantic-tok"


# ---------------------------------------------------------------------------
# build_and_run config parsing
# ---------------------------------------------------------------------------


class TestBuildAndRunConfigParsing:
    """Test that build_and_run correctly parses and validates config values."""

    @pytest.fixture()
    def _patch_loop(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        async def fake_run_main_loop(cfg: DiscordBridgeConfig, **kwargs: Any) -> None:
            captured["cfg"] = cfg
            captured["kwargs"] = kwargs

        class FakeBot:
            def __init__(self, token: str, *, guild_id: int | None = None) -> None:
                self.token = token
                self.guild_id = guild_id

        monkeypatch.setattr(discord_backend, "run_main_loop", fake_run_main_loop)
        monkeypatch.setattr(discord_backend, "DiscordBotClient", FakeBot)
        monkeypatch.setattr(discord_backend, "DiscordTransport", lambda bot: MagicMock())
        monkeypatch.setattr(
            discord_backend,
            "DiscordPresenter",
            lambda **kw: MagicMock(),
        )
        return captured

    def _run(
        self,
        settings: dict[str, Any],
        *,
        runtime: TransportRuntime | None = None,
        tmp_path: Path | None = None,
    ) -> None:
        if runtime is None:
            runtime = _make_runtime()
        DiscordBackend().build_and_run(
            transport_config=settings,
            config_path=Path(tmp_path or "/tmp") / "tunapi.toml",
            runtime=runtime,
            final_notify=False,
            default_engine_override=None,
        )

    # --- trigger_mode_default validation ---

    def test_trigger_mode_default_all(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run({"bot_token": "t", "trigger_mode_default": "all"}, tmp_path=tmp_path)
        assert _patch_loop["cfg"].trigger_mode_default == "all"

    def test_trigger_mode_default_mentions(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run(
            {"bot_token": "t", "trigger_mode_default": "mentions"}, tmp_path=tmp_path
        )
        assert _patch_loop["cfg"].trigger_mode_default == "mentions"

    def test_trigger_mode_default_case_insensitive(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run(
            {"bot_token": "t", "trigger_mode_default": "  ALL  "}, tmp_path=tmp_path
        )
        assert _patch_loop["cfg"].trigger_mode_default == "all"

    def test_trigger_mode_default_invalid_falls_back_to_all(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run(
            {"bot_token": "t", "trigger_mode_default": "bogus"}, tmp_path=tmp_path
        )
        assert _patch_loop["cfg"].trigger_mode_default == "all"

    def test_trigger_mode_default_non_string_falls_back(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run(
            {"bot_token": "t", "trigger_mode_default": 999}, tmp_path=tmp_path
        )
        assert _patch_loop["cfg"].trigger_mode_default == "all"

    # --- allowed_user_ids normalization ---

    def test_allowed_user_ids_list_of_ints(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run(
            {"bot_token": "t", "allowed_user_ids": [100, 200]}, tmp_path=tmp_path
        )
        assert _patch_loop["cfg"].allowed_user_ids == frozenset({100, 200})

    def test_allowed_user_ids_none(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run({"bot_token": "t"}, tmp_path=tmp_path)
        assert _patch_loop["cfg"].allowed_user_ids is None

    def test_allowed_user_ids_invalid_type_becomes_none(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run(
            {"bot_token": "t", "allowed_user_ids": "not-a-list"}, tmp_path=tmp_path
        )
        assert _patch_loop["cfg"].allowed_user_ids is None

    # --- files settings parsing ---

    def test_files_defaults(self, _patch_loop: dict, tmp_path: Path) -> None:
        self._run({"bot_token": "t"}, tmp_path=tmp_path)
        files = _patch_loop["cfg"].files
        assert files.enabled is False
        assert files.auto_put is True
        assert files.auto_put_mode == "upload"
        assert files.uploads_dir == "incoming"
        assert files.max_upload_bytes == 20 * 1024 * 1024
        assert files.allowed_user_ids is None

    def test_files_custom_values(self, _patch_loop: dict, tmp_path: Path) -> None:
        self._run(
            {
                "bot_token": "t",
                "files": {
                    "enabled": True,
                    "auto_put": False,
                    "auto_put_mode": "prompt",
                    "uploads_dir": "uploads",
                    "max_upload_bytes": 1024,
                    "deny_globs": [".secret"],
                    "allowed_user_ids": [42],
                },
            },
            tmp_path=tmp_path,
        )
        files = _patch_loop["cfg"].files
        assert files.enabled is True
        assert files.auto_put is False
        assert files.auto_put_mode == "prompt"
        assert files.uploads_dir == "uploads"
        assert files.max_upload_bytes == 1024
        assert files.deny_globs == (".secret",)
        assert files.allowed_user_ids == frozenset({42})

    # --- voice settings parsing ---

    def test_voice_defaults(self, _patch_loop: dict, tmp_path: Path) -> None:
        self._run({"bot_token": "t"}, tmp_path=tmp_path)
        voice = _patch_loop["cfg"].voice_messages
        assert voice.enabled is False
        assert voice.max_bytes == 10 * 1024 * 1024
        assert voice.whisper_model == "base"

    def test_voice_custom_values(self, _patch_loop: dict, tmp_path: Path) -> None:
        self._run(
            {
                "bot_token": "t",
                "voice_messages": {
                    "enabled": True,
                    "max_bytes": 5000,
                    "whisper_model": "large-v3",
                },
            },
            tmp_path=tmp_path,
        )
        voice = _patch_loop["cfg"].voice_messages
        assert voice.enabled is True
        assert voice.max_bytes == 5000
        assert voice.whisper_model == "large-v3"

    # --- session_mode and show_resume_line ---

    def test_session_mode_and_resume_line(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run(
            {
                "bot_token": "t",
                "session_mode": "chat",
                "show_resume_line": False,
            },
            tmp_path=tmp_path,
        )
        assert _patch_loop["cfg"].session_mode == "chat"
        assert _patch_loop["cfg"].show_resume_line is False

    # --- message_overflow ---

    def test_message_overflow_trim(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run(
            {"bot_token": "t", "message_overflow": "trim"}, tmp_path=tmp_path
        )
        assert _patch_loop["cfg"].message_overflow == "trim"

    # --- media_group_debounce_s ---

    def test_media_group_debounce_default(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run({"bot_token": "t"}, tmp_path=tmp_path)
        assert _patch_loop["cfg"].media_group_debounce_s == 0.75

    def test_media_group_debounce_custom(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run(
            {"bot_token": "t", "media_group_debounce_s": 1.5}, tmp_path=tmp_path
        )
        assert _patch_loop["cfg"].media_group_debounce_s == 1.5

    def test_media_group_debounce_invalid_string_falls_back(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run(
            {"bot_token": "t", "media_group_debounce_s": "bad"}, tmp_path=tmp_path
        )
        assert _patch_loop["cfg"].media_group_debounce_s == 0.75

    def test_media_group_debounce_negative_falls_back(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run(
            {"bot_token": "t", "media_group_debounce_s": -1.0}, tmp_path=tmp_path
        )
        assert _patch_loop["cfg"].media_group_debounce_s == 0.75

    # --- guild_id ---

    def test_guild_id_passed_through(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run(
            {"bot_token": "t", "guild_id": 12345}, tmp_path=tmp_path
        )
        assert _patch_loop["cfg"].guild_id == 12345

    def test_guild_id_default_none(
        self, _patch_loop: dict, tmp_path: Path
    ) -> None:
        self._run({"bot_token": "t"}, tmp_path=tmp_path)
        assert _patch_loop["cfg"].guild_id is None


# ---------------------------------------------------------------------------
# DiscordBackend metadata
# ---------------------------------------------------------------------------


class TestDiscordBackendMeta:
    def test_id_and_description(self) -> None:
        backend = DiscordBackend()
        assert backend.id == "discord"
        assert backend.description == "Discord bot"
