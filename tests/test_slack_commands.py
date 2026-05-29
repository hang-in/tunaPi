"""Comprehensive tests for tunapi.slack.commands — all handler functions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from tunapi.context import RunContext
from tunapi.core.chat_command_handlers import _MIN_PREFIX_LEN, _resolve_id
from tunapi.slack.commands import (
    handle_branch,
    handle_cancel,
    handle_context,
    handle_help,
    handle_memory,
    handle_model,
    handle_models,
    handle_persona,
    handle_project,
    handle_review,
    handle_rt,
    handle_status,
    handle_trigger,
    parse_command,
)
from tunapi.transport import RenderedMessage

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _make_send() -> AsyncMock:
    return AsyncMock()


def _sent_text(send: AsyncMock) -> str:
    """Return the text from the first send() call."""
    assert send.await_count >= 1
    msg: RenderedMessage = send.call_args_list[0].args[0]
    return msg.text


def _make_runtime(
    engines: list[str] | None = None,
    projects: list[str] | None = None,
    default_engine: str = "claude",
) -> MagicMock:
    rt = MagicMock()
    rt.available_engine_ids.return_value = (
        ["claude", "codex"] if engines is None else engines
    )
    rt.project_aliases.return_value = [] if projects is None else projects
    rt.default_engine = default_engine
    rt.normalize_project_key.return_value = None  # default: unknown project
    rt.roundtable = MagicMock()
    rt.roundtable.engines = []
    rt.roundtable.rounds = 1
    rt.roundtable.max_rounds = 3
    return rt


def _make_chat_prefs(**overrides: Any) -> AsyncMock:
    prefs = AsyncMock()
    prefs.get_default_engine = AsyncMock(return_value=overrides.get("engine"))
    prefs.get_trigger_mode = AsyncMock(return_value=overrides.get("trigger"))
    prefs.get_engine_model = AsyncMock(return_value=overrides.get("model"))
    prefs.get_all_engine_models = AsyncMock(
        return_value=overrides.get("all_models", {})
    )
    prefs.get_context = AsyncMock(return_value=overrides.get("context"))
    return prefs


@dataclass
class FakeEntry:
    id: str
    type: str
    title: str
    content: str = ""
    tags: list[str] | None = None
    timestamp: float = 1700000000.0
    source: str = "user"


@dataclass
class FakeBranch:
    branch_id: str
    label: str
    status: str = "active"
    git_branch: str | None = None


@dataclass
class FakeReview:
    review_id: str
    artifact_id: str
    artifact_version: int = 1
    status: str = "pending"
    created_at: str = "2025-01-01"


@dataclass
class FakePersona:
    name: str
    prompt: str


# ---------------------------------------------------------------------------
# Utility: extract help commands
# ---------------------------------------------------------------------------


def _extract_help_commands(help_text: str) -> set[str]:
    """Extract command names from help table rows (lines starting with |)."""
    commands: set[str] = set()
    for line in help_text.splitlines():
        if line.startswith("|") and "`!" in line:
            matches = re.findall(r"`!(\w+)", line)
            commands.update(matches)
    return commands


# The commands that _try_dispatch_command() actually handles
_DISPATCHER_COMMANDS = {
    "new",
    "help",
    "model",
    "models",
    "trigger",
    "project",
    "persona",
    "memory",
    "branch",
    "review",
    "context",
    "rt",
    "file",
    "status",
    "cancel",
}


def _get_help_text() -> str:
    """Run handle_help and return the captured text."""
    captured: list[str] = []

    async def fake_send(msg: RenderedMessage) -> None:
        captured.append(msg.text)

    class FakeRuntime:
        def available_engine_ids(self):
            return ["claude"]

        def project_aliases(self):
            return []

    async def _run():
        await handle_help(runtime=FakeRuntime(), send=fake_send)

    anyio.run(_run)
    assert captured
    return captured[0]


class TestHelpDispatcherConsistency:
    def test_help_commands_match_dispatcher(self):
        """Every command in help text must have a dispatcher case."""
        help_text = _get_help_text()
        help_commands = _extract_help_commands(help_text)
        assert help_commands, "should have extracted at least one command"

        undispatched = help_commands - _DISPATCHER_COMMANDS
        assert not undispatched, (
            f"Commands in help but not in dispatcher: {undispatched}"
        )

    def test_rt_in_help(self):
        """!rt should appear in help."""
        assert "!rt" in _get_help_text()

    def test_file_in_help(self):
        """!file should appear in help."""
        assert "!file" in _get_help_text()


class TestParseCommand:
    def test_slash_command_not_parsed(self):
        cmd, args = parse_command("/help")
        assert cmd is None

    def test_bang_command(self):
        cmd, args = parse_command("!model claude")
        assert cmd == "model"
        assert args == "claude"

    def test_no_command(self):
        cmd, args = parse_command("hello world")
        assert cmd is None

    def test_empty(self):
        cmd, args = parse_command("")
        assert cmd is None

    def test_bang_command_no_args(self):
        cmd, args = parse_command("!help")
        assert cmd == "help"
        assert args == ""

    def test_bang_command_with_extra_spaces(self):
        cmd, args = parse_command("!model   claude   opus")
        assert cmd == "model"
        # parse_command preserves trailing args as-is after first split
        assert "claude" in args


class TestDoctorChannelIdContract:
    def test_doctor_accepts_allowed_channel_ids(self):
        """doctor slack checks must accept allowed_channel_ids parameter."""
        import inspect

        from tunapi.cli.doctor import _doctor_slack_checks

        sig = inspect.signature(_doctor_slack_checks)
        params = list(sig.parameters.keys())
        assert "allowed_channel_ids" in params


# ---------------------------------------------------------------------------
# !help
# ---------------------------------------------------------------------------


class TestHandleHelp:
    async def test_help_basic(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude", "codex"], projects=["myproj"])
        await handle_help(runtime=runtime, send=send)
        text = _sent_text(send)
        assert "tunapi commands" in text
        assert "`claude`" in text
        assert "`codex`" in text
        assert "`myproj`" in text

    async def test_help_no_engines_no_projects(self):
        send = _make_send()
        runtime = _make_runtime(engines=[], projects=[])
        await handle_help(runtime=runtime, send=send)
        text = _sent_text(send)
        assert "none" in text

    async def test_help_lists_all_commands(self):
        send = _make_send()
        runtime = _make_runtime()
        await handle_help(runtime=runtime, send=send)
        text = _sent_text(send)
        for cmd in (
            "help",
            "new",
            "model",
            "models",
            "trigger",
            "project",
            "persona",
            "memory",
            "branch",
            "review",
            "context",
            "rt",
            "file",
            "status",
            "cancel",
        ):
            assert f"!{cmd}" in text, f"!{cmd} missing from help"

    async def test_help_contains_table(self):
        send = _make_send()
        runtime = _make_runtime()
        await handle_help(runtime=runtime, send=send)
        text = _sent_text(send)
        assert "| Command | Description |" in text

    async def test_help_projects_sorted(self):
        send = _make_send()
        runtime = _make_runtime(projects=["Zeta", "alpha", "Beta"])
        await handle_help(runtime=runtime, send=send)
        text = _sent_text(send)
        # Should be sorted case-insensitively
        assert "`alpha`" in text
        assert "`Beta`" in text
        assert "`Zeta`" in text


# ---------------------------------------------------------------------------
# !model
# ---------------------------------------------------------------------------


class TestHandleModel:
    async def test_no_args_shows_current(self):
        send = _make_send()
        runtime = _make_runtime()
        prefs = _make_chat_prefs(engine="claude")
        await handle_model(
            "", channel_id="ch1", runtime=runtime, chat_prefs=prefs, send=send
        )
        text = _sent_text(send)
        assert "Current engine" in text
        assert "`claude`" in text

    async def test_no_args_with_model_override(self):
        send = _make_send()
        runtime = _make_runtime()
        prefs = _make_chat_prefs(engine="claude", model="claude-opus-4-20250514")
        await handle_model(
            "", channel_id="ch1", runtime=runtime, chat_prefs=prefs, send=send
        )
        text = _sent_text(send)
        assert "Model:" in text
        assert "claude-opus-4-20250514" in text

    async def test_no_args_no_prefs(self):
        send = _make_send()
        runtime = _make_runtime(default_engine="codex")
        await handle_model(
            "", channel_id="ch1", runtime=runtime, chat_prefs=None, send=send
        )
        text = _sent_text(send)
        assert "`codex`" in text

    async def test_no_args_shows_usage(self):
        send = _make_send()
        runtime = _make_runtime()
        await handle_model(
            "", channel_id="ch1", runtime=runtime, chat_prefs=None, send=send
        )
        text = _sent_text(send)
        assert "Usage" in text

    async def test_unknown_engine(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude"])
        await handle_model(
            "nope", channel_id="ch1", runtime=runtime, chat_prefs=None, send=send
        )
        text = _sent_text(send)
        assert "Unknown engine" in text
        assert "`nope`" in text

    async def test_set_engine_only(self):
        send = _make_send()
        runtime = _make_runtime()
        prefs = _make_chat_prefs()
        await handle_model(
            "claude", channel_id="ch1", runtime=runtime, chat_prefs=prefs, send=send
        )
        prefs.set_default_engine.assert_awaited_once_with("ch1", "claude")
        text = _sent_text(send)
        assert "Default engine set to `claude`" in text

    async def test_set_engine_case_insensitive(self):
        send = _make_send()
        runtime = _make_runtime(engines=["Claude"])
        prefs = _make_chat_prefs()
        await handle_model(
            "claude", channel_id="ch1", runtime=runtime, chat_prefs=prefs, send=send
        )
        prefs.set_default_engine.assert_awaited_once_with("ch1", "Claude")

    async def test_set_engine_without_prefs(self):
        send = _make_send()
        runtime = _make_runtime()
        await handle_model(
            "claude", channel_id="ch1", runtime=runtime, chat_prefs=None, send=send
        )
        text = _sent_text(send)
        assert "Default engine set to `claude`" in text

    async def test_set_engine_shows_existing_model(self):
        send = _make_send()
        runtime = _make_runtime()
        prefs = _make_chat_prefs(model="some-model")
        await handle_model(
            "claude", channel_id="ch1", runtime=runtime, chat_prefs=prefs, send=send
        )
        text = _sent_text(send)
        assert "model:" in text
        assert "some-model" in text

    async def test_set_model(self):
        send = _make_send()
        runtime = _make_runtime()
        prefs = _make_chat_prefs()
        await handle_model(
            "claude claude-opus-4-20250514",
            channel_id="ch1",
            runtime=runtime,
            chat_prefs=prefs,
            send=send,
        )
        prefs.set_engine_model.assert_awaited_once_with(
            "ch1", "claude", "claude-opus-4-20250514"
        )
        text = _sent_text(send)
        assert "claude-opus-4-20250514" in text

    async def test_set_model_without_prefs(self):
        send = _make_send()
        runtime = _make_runtime()
        await handle_model(
            "claude my-model",
            channel_id="ch1",
            runtime=runtime,
            chat_prefs=None,
            send=send,
        )
        text = _sent_text(send)
        assert "Model for `claude` set to `my-model`" in text

    async def test_clear_model(self):
        send = _make_send()
        runtime = _make_runtime()
        prefs = _make_chat_prefs()
        await handle_model(
            "claude clear",
            channel_id="ch1",
            runtime=runtime,
            chat_prefs=prefs,
            send=send,
        )
        prefs.clear_engine_model.assert_awaited_once_with("ch1", "claude")
        text = _sent_text(send)
        assert "cleared" in text

    async def test_clear_model_without_prefs(self):
        send = _make_send()
        runtime = _make_runtime()
        await handle_model(
            "claude clear",
            channel_id="ch1",
            runtime=runtime,
            chat_prefs=None,
            send=send,
        )
        text = _sent_text(send)
        assert "cleared" in text


# ---------------------------------------------------------------------------
# !models
# ---------------------------------------------------------------------------


class TestHandleModels:
    async def test_lists_all_engines(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude", "codex"])
        await handle_models(
            "", channel_id="ch1", runtime=runtime, chat_prefs=None, send=send
        )
        text = _sent_text(send)
        assert "Available Models" in text

    async def test_unknown_engine(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude"])
        await handle_models(
            "nope", channel_id="ch1", runtime=runtime, chat_prefs=None, send=send
        )
        text = _sent_text(send)
        assert "Unknown engine" in text

    async def test_specific_engine(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude", "codex"])
        await handle_models(
            "claude", channel_id="ch1", runtime=runtime, chat_prefs=None, send=send
        )
        text = _sent_text(send)
        assert "claude" in text

    async def test_with_current_model(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude"])
        prefs = _make_chat_prefs(all_models={"claude": "opus-4"})
        await handle_models(
            "claude", channel_id="ch1", runtime=runtime, chat_prefs=prefs, send=send
        )
        text = _sent_text(send)
        assert "current" in text
        assert "opus-4" in text

    async def test_shows_set_usage(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude"])
        await handle_models(
            "", channel_id="ch1", runtime=runtime, chat_prefs=None, send=send
        )
        text = _sent_text(send)
        assert "!model <engine> <model>" in text


# ---------------------------------------------------------------------------
# !trigger
# ---------------------------------------------------------------------------


class TestHandleTrigger:
    async def test_set_all(self):
        send = _make_send()
        prefs = _make_chat_prefs()
        await handle_trigger("all", channel_id="ch1", chat_prefs=prefs, send=send)
        prefs.set_trigger_mode.assert_awaited_once_with("ch1", "all")
        assert "respond to all messages" in _sent_text(send)

    async def test_set_mentions(self):
        send = _make_send()
        prefs = _make_chat_prefs()
        await handle_trigger("mentions", channel_id="ch1", chat_prefs=prefs, send=send)
        prefs.set_trigger_mode.assert_awaited_once_with("ch1", "mentions")
        assert "respond only when @mentioned" in _sent_text(send)

    async def test_invalid_shows_current(self):
        send = _make_send()
        prefs = _make_chat_prefs(trigger="all")
        await handle_trigger("invalid", channel_id="ch1", chat_prefs=prefs, send=send)
        text = _sent_text(send)
        assert "Current trigger mode" in text
        assert "`all`" in text

    async def test_no_prefs(self):
        send = _make_send()
        await handle_trigger("all", channel_id="ch1", chat_prefs=None, send=send)
        assert "respond to all messages" in _sent_text(send)

    async def test_empty_args_shows_usage(self):
        send = _make_send()
        prefs = _make_chat_prefs()
        await handle_trigger("", channel_id="ch1", chat_prefs=prefs, send=send)
        assert "Usage" in _sent_text(send)

    async def test_invalid_no_prefs_defaults_to_mentions(self):
        send = _make_send()
        await handle_trigger("bad", channel_id="ch1", chat_prefs=None, send=send)
        text = _sent_text(send)
        assert "`mentions`" in text


# ---------------------------------------------------------------------------
# !status
# ---------------------------------------------------------------------------


class TestHandleStatus:
    async def test_basic_status(self):
        send = _make_send()
        runtime = _make_runtime(default_engine="claude")
        prefs = _make_chat_prefs(engine="codex", trigger="all")
        await handle_status(
            channel_id="ch1",
            runtime=runtime,
            chat_prefs=prefs,
            session_engine=None,
            has_session=False,
            send=send,
        )
        text = _sent_text(send)
        assert "Session status" in text
        assert "`codex`" in text
        assert "`all`" in text
        assert "`ch1`" in text

    async def test_status_no_prefs(self):
        send = _make_send()
        runtime = _make_runtime(default_engine="claude")
        await handle_status(
            channel_id="ch1",
            runtime=runtime,
            chat_prefs=None,
            session_engine=None,
            has_session=False,
            send=send,
        )
        text = _sent_text(send)
        assert "`claude`" in text

    async def test_status_with_project(self):
        send = _make_send()
        runtime = _make_runtime()
        ctx = RunContext(project="myproj", branch="feat-x")
        prefs = _make_chat_prefs(context=ctx)
        await handle_status(
            channel_id="ch1",
            runtime=runtime,
            chat_prefs=prefs,
            session_engine=None,
            has_session=False,
            send=send,
        )
        text = _sent_text(send)
        assert "`myproj`" in text
        assert "feat-x" in text

    async def test_status_active_session(self):
        send = _make_send()
        runtime = _make_runtime()
        await handle_status(
            channel_id="ch1",
            runtime=runtime,
            chat_prefs=None,
            session_engine="claude",
            has_session=True,
            send=send,
        )
        text = _sent_text(send)
        assert "active" in text

    async def test_status_no_session(self):
        send = _make_send()
        runtime = _make_runtime()
        await handle_status(
            channel_id="ch1",
            runtime=runtime,
            chat_prefs=None,
            session_engine=None,
            has_session=False,
            send=send,
        )
        text = _sent_text(send)
        assert "none" in text

    async def test_status_project_without_branch(self):
        send = _make_send()
        runtime = _make_runtime()
        ctx = RunContext(project="myproj")
        prefs = _make_chat_prefs(context=ctx)
        await handle_status(
            channel_id="ch1",
            runtime=runtime,
            chat_prefs=prefs,
            session_engine=None,
            has_session=False,
            send=send,
        )
        text = _sent_text(send)
        assert "`myproj`" in text
        assert "feat" not in text


# ---------------------------------------------------------------------------
# !project
# ---------------------------------------------------------------------------


class TestHandleProject:
    async def _call(self, args: str, **kwargs: Any) -> str:
        send = _make_send()
        defaults: dict[str, Any] = {
            "channel_id": "ch1",
            "runtime": _make_runtime(),
            "chat_prefs": None,
            "projects_root": None,
            "config_path": None,
            "send": send,
        }
        defaults.update(kwargs)
        await handle_project(args, **defaults)
        return _sent_text(send)

    async def test_unknown_subcmd(self):
        text = await self._call("foo")
        assert "Usage" in text

    async def test_list_no_projects(self):
        text = await self._call("list")
        assert "No projects found" in text

    async def test_list_with_configured(self):
        runtime = _make_runtime(projects=["alpha", "beta"])
        text = await self._call("list", runtime=runtime)
        assert "Configured" in text
        assert "`alpha`" in text

    async def test_set_missing_name(self):
        text = await self._call("set")
        assert "Usage" in text

    async def test_set_unknown_project(self):
        text = await self._call("set myproj")
        assert "Unknown project" in text

    async def test_set_known_project(self):
        runtime = _make_runtime()
        runtime.normalize_project_key.return_value = "myproj"
        prefs = _make_chat_prefs()
        text = await self._call(
            "set myproj",
            runtime=runtime,
            chat_prefs=prefs,
        )
        assert "Project set to `myproj`" in text
        prefs.set_context.assert_awaited_once()

    async def test_set_known_project_no_prefs(self):
        runtime = _make_runtime()
        runtime.normalize_project_key.return_value = "myproj"
        text = await self._call("set myproj", runtime=runtime)
        assert "Project set to `myproj`" in text

    async def test_info_no_context(self):
        text = await self._call("info")
        assert "No project bound" in text

    async def test_info_with_context(self):
        prefs = _make_chat_prefs(
            context=RunContext(project="myproj", branch="dev"),
        )
        text = await self._call("info", chat_prefs=prefs)
        assert "`myproj`" in text
        assert "`dev`" in text

    async def test_info_with_project_no_branch(self):
        prefs = _make_chat_prefs(
            context=RunContext(project="myproj"),
        )
        text = await self._call("info", chat_prefs=prefs)
        assert "`myproj`" in text

    async def test_list_discovered_projects(self, tmp_path):
        """Discovered projects from projects_root appear in list."""
        # Create a fake discovered project with .git directory
        proj_dir = tmp_path / "discovered_proj"
        proj_dir.mkdir()
        (proj_dir / ".git").mkdir()

        runtime = _make_runtime(projects=[])
        text = await self._call(
            "list",
            runtime=runtime,
            projects_root=str(tmp_path),
        )
        assert "Discovered" in text
        assert "discovered_proj" in text

    async def test_empty_subcmd(self):
        text = await self._call("")
        assert "Usage" in text


# ---------------------------------------------------------------------------
# !persona
# ---------------------------------------------------------------------------


class TestHandlePersona:
    async def _call(self, args: str, prefs: Any = None) -> str:
        send = _make_send()
        await handle_persona(args, chat_prefs=prefs, send=send)
        return _sent_text(send)

    async def test_no_prefs(self):
        text = await self._call("")
        assert "unavailable" in text

    async def test_unknown_subcmd(self):
        prefs = AsyncMock()
        text = await self._call("xyz", prefs=prefs)
        assert "Usage" in text

    async def test_add_missing_args(self):
        prefs = AsyncMock()
        text = await self._call("add", prefs=prefs)
        assert "Usage" in text

    async def test_add_missing_prompt(self):
        prefs = AsyncMock()
        text = await self._call("add myname", prefs=prefs)
        assert "Usage" in text

    async def test_add_success(self):
        prefs = AsyncMock()
        text = await self._call('add reviewer "Be critical"', prefs=prefs)
        prefs.add_persona.assert_awaited_once_with("reviewer", "Be critical")
        assert "Persona `reviewer` added" in text

    async def test_add_strips_quotes(self):
        prefs = AsyncMock()
        await self._call("add mybot 'Be helpful'", prefs=prefs)
        prefs.add_persona.assert_awaited_once_with("mybot", "Be helpful")

    async def test_add_empty_prompt_after_strip(self):
        prefs = AsyncMock()
        text = await self._call('add mybot ""', prefs=prefs)
        assert "Usage" in text

    async def test_list_empty(self):
        prefs = AsyncMock()
        prefs.list_personas = AsyncMock(return_value={})
        text = await self._call("list", prefs=prefs)
        assert "No personas defined" in text

    async def test_list_with_personas(self):
        prefs = AsyncMock()
        prefs.list_personas = AsyncMock(
            return_value={
                "critic": FakePersona("critic", "Be very critical"),
                "helper": FakePersona("helper", "Be helpful and kind"),
            }
        )
        text = await self._call("list", prefs=prefs)
        assert "*critic*" in text
        assert "*helper*" in text

    async def test_list_truncates_long_prompt(self):
        prefs = AsyncMock()
        long_prompt = "x" * 100
        prefs.list_personas = AsyncMock(
            return_value={
                "verbose": FakePersona("verbose", long_prompt),
            }
        )
        text = await self._call("list", prefs=prefs)
        assert "..." in text

    async def test_remove_missing_name(self):
        prefs = AsyncMock()
        text = await self._call("remove", prefs=prefs)
        assert "Usage" in text

    async def test_remove_success(self):
        prefs = AsyncMock()
        prefs.remove_persona = AsyncMock(return_value=True)
        text = await self._call("remove critic", prefs=prefs)
        assert "removed" in text

    async def test_remove_not_found(self):
        prefs = AsyncMock()
        prefs.remove_persona = AsyncMock(return_value=False)
        text = await self._call("remove nope", prefs=prefs)
        assert "not found" in text

    async def test_show_missing_name(self):
        prefs = AsyncMock()
        text = await self._call("show", prefs=prefs)
        assert "Usage" in text

    async def test_show_found(self):
        prefs = AsyncMock()
        prefs.get_persona = AsyncMock(return_value=FakePersona("critic", "Be critical"))
        text = await self._call("show critic", prefs=prefs)
        assert "*critic*" in text
        assert "Be critical" in text

    async def test_show_not_found(self):
        prefs = AsyncMock()
        prefs.get_persona = AsyncMock(return_value=None)
        text = await self._call("show nope", prefs=prefs)
        assert "not found" in text


# ---------------------------------------------------------------------------
# !memory
# ---------------------------------------------------------------------------


class TestHandleMemory:
    async def _call(
        self,
        args: str,
        project: str | None = "proj",
        facade: Any = None,
        engine: str | None = None,
    ) -> str:
        send = _make_send()
        await handle_memory(
            args,
            project=project,
            facade=facade,
            current_engine=engine,
            send=send,
        )
        return _sent_text(send)

    async def test_no_project(self):
        text = await self._call("", project=None)
        assert "프로젝트를 먼저" in text

    async def test_no_facade(self):
        text = await self._call("", project="proj", facade=None)
        assert "unavailable" in text

    async def test_summary_empty(self):
        facade = MagicMock()
        facade.memory.get_context_summary = AsyncMock(return_value="")
        text = await self._call("", facade=facade)
        assert "메모리가 없습니다" in text

    async def test_summary_with_content(self):
        facade = MagicMock()
        facade.memory.get_context_summary = AsyncMock(return_value="some summary")
        text = await self._call("", facade=facade)
        assert text == "some summary"

    async def test_list_entries(self):
        facade = MagicMock()
        facade.memory.list_entries = AsyncMock(
            return_value=[
                FakeEntry(id="abc123def456ghij", type="decision", title="Use pytest"),
            ]
        )
        text = await self._call("list", facade=facade)
        assert "Memory — proj" in text
        assert "decision" in text
        assert "Use pytest" in text

    async def test_list_empty(self):
        facade = MagicMock()
        facade.memory.list_entries = AsyncMock(return_value=[])
        text = await self._call("list", facade=facade)
        assert "No entries" in text

    async def test_list_invalid_type(self):
        facade = MagicMock()
        text = await self._call("list invalid", facade=facade)
        assert "Unknown type" in text

    async def test_list_valid_types(self):
        for t in ("decision", "review", "idea", "context"):
            facade = MagicMock()
            facade.memory.list_entries = AsyncMock(return_value=[])
            text = await self._call(f"list {t}", facade=facade)
            assert "Unknown type" not in text

    async def test_list_with_tags(self):
        facade = MagicMock()
        facade.memory.list_entries = AsyncMock(
            return_value=[
                FakeEntry(
                    id="abc123def456ghij",
                    type="decision",
                    title="Tagged",
                    tags=["important", "p0"],
                ),
            ]
        )
        text = await self._call("list", facade=facade)
        assert "important" in text
        assert "p0" in text

    async def test_add_missing_args(self):
        facade = MagicMock()
        text = await self._call("add decision", facade=facade)
        assert "Usage" in text

    async def test_add_invalid_type(self):
        facade = MagicMock()
        text = await self._call("add bogus title content", facade=facade)
        assert "Unknown type" in text

    async def test_add_success(self):
        facade = MagicMock()
        facade.memory.add_entry = AsyncMock(
            return_value=FakeEntry(
                id="newid12345678",
                type="decision",
                title="Use ruff",
            )
        )
        text = await self._call(
            "add decision Use-ruff Because-it-is-fast", facade=facade, engine="claude"
        )
        assert "Entry added" in text
        assert "decision" in text
        facade.memory.add_entry.assert_awaited_once()

    async def test_add_uses_default_source(self):
        facade = MagicMock()
        facade.memory.add_entry = AsyncMock(
            return_value=FakeEntry(
                id="newid12345678",
                type="idea",
                title="Test",
            )
        )
        await self._call("add idea Test Content", facade=facade, engine=None)
        call_kwargs = facade.memory.add_entry.call_args.kwargs
        assert call_kwargs["source"] == "user"

    async def test_add_uses_engine_as_source(self):
        facade = MagicMock()
        facade.memory.add_entry = AsyncMock(
            return_value=FakeEntry(
                id="newid12345678",
                type="idea",
                title="Test",
            )
        )
        await self._call("add idea Test Content", facade=facade, engine="gemini")
        call_kwargs = facade.memory.add_entry.call_args.kwargs
        assert call_kwargs["source"] == "gemini"

    async def test_search_missing_query(self):
        facade = MagicMock()
        text = await self._call("search", facade=facade)
        assert "Usage" in text

    async def test_search_no_results(self):
        facade = MagicMock()
        facade.memory.search = AsyncMock(return_value=[])
        text = await self._call("search foo", facade=facade)
        assert "No results" in text

    async def test_search_with_results(self):
        facade = MagicMock()
        facade.memory.search = AsyncMock(
            return_value=[
                FakeEntry(id="abc123def456ghij", type="idea", title="Cool idea"),
            ]
        )
        text = await self._call("search cool", facade=facade)
        assert "Search results" in text
        assert "Cool idea" in text

    async def test_search_limits_to_10(self):
        facade = MagicMock()
        entries = [
            FakeEntry(id=f"entry{i:012d}", type="idea", title=f"Idea {i}")
            for i in range(15)
        ]
        facade.memory.search = AsyncMock(return_value=entries)
        text = await self._call("search test", facade=facade)
        # Should show at most 10
        assert "Idea 9" in text
        assert "Idea 10" not in text

    async def test_delete_missing_id(self):
        facade = MagicMock()
        text = await self._call("delete", facade=facade)
        assert "Usage" in text

    async def test_delete_prefix_too_short(self):
        facade = MagicMock()
        text = await self._call("delete abc", facade=facade)
        assert "too short" in text

    async def test_delete_success(self):
        facade = MagicMock()
        full_id = "abcdef1234567890"
        facade.memory.list_entries = AsyncMock(
            return_value=[
                FakeEntry(id=full_id, type="decision", title="Old decision"),
            ]
        )
        facade.memory.delete_entry = AsyncMock(return_value=True)
        text = await self._call(f"delete {full_id[:8]}", facade=facade)
        assert "deleted" in text

    async def test_delete_not_found(self):
        facade = MagicMock()
        facade.memory.list_entries = AsyncMock(
            return_value=[
                FakeEntry(id="abcdef1234567890", type="decision", title="Old"),
            ]
        )
        facade.memory.delete_entry = AsyncMock(return_value=False)
        text = await self._call("delete abcdef1234567890", facade=facade)
        assert "not found" in text

    async def test_unknown_subcmd(self):
        facade = MagicMock()
        text = await self._call("bogus", facade=facade)
        assert "Usage" in text


# ---------------------------------------------------------------------------
# !branch
# ---------------------------------------------------------------------------


class TestHandleBranch:
    async def _call(
        self,
        args: str,
        project: str | None = "proj",
        facade: Any = None,
    ) -> str:
        send = _make_send()
        await handle_branch(args, project=project, facade=facade, send=send)
        return _sent_text(send)

    async def test_no_project(self):
        text = await self._call("", project=None)
        assert "프로젝트를 먼저" in text

    async def test_no_facade(self):
        text = await self._call("", project="proj", facade=None)
        assert "unavailable" in text

    async def test_active_branches_empty(self):
        facade = MagicMock()
        facade.conv_branches.list = AsyncMock(return_value=[])
        text = await self._call("", facade=facade)
        assert "활성 대화 분기가 없습니다" in text

    async def test_active_branches(self):
        facade = MagicMock()
        facade.conv_branches.list = AsyncMock(
            return_value=[
                FakeBranch(branch_id="br12345678901234", label="experiment"),
            ]
        )
        text = await self._call("", facade=facade)
        assert "Active branches" in text
        assert "experiment" in text

    async def test_active_branches_with_git(self):
        facade = MagicMock()
        facade.conv_branches.list = AsyncMock(
            return_value=[
                FakeBranch(
                    branch_id="br12345678901234",
                    label="experiment",
                    git_branch="feat/exp",
                ),
            ]
        )
        text = await self._call("", facade=facade)
        assert "feat/exp" in text

    async def test_create_missing_label(self):
        facade = MagicMock()
        text = await self._call("create", facade=facade)
        assert "Usage" in text

    async def test_create_success(self):
        facade = MagicMock()
        facade.conv_branches.create = AsyncMock(
            return_value=FakeBranch(branch_id="newbranch12345678", label="my-branch"),
        )
        text = await self._call("create my-branch", facade=facade)
        assert "Branch created" in text
        assert "my-branch" in text

    async def test_list_invalid_status(self):
        facade = MagicMock()
        text = await self._call("list invalid", facade=facade)
        assert "Unknown status" in text

    async def test_list_empty(self):
        facade = MagicMock()
        facade.conv_branches.list = AsyncMock(return_value=[])
        text = await self._call("list active", facade=facade)
        assert "No branches" in text

    async def test_list_all_statuses(self):
        for s in ("active", "merged", "discarded"):
            facade = MagicMock()
            facade.conv_branches.list = AsyncMock(return_value=[])
            text = await self._call(f"list {s}", facade=facade)
            assert "Unknown status" not in text

    async def test_list_with_branches(self):
        facade = MagicMock()
        facade.conv_branches.list = AsyncMock(
            return_value=[
                FakeBranch(
                    branch_id="br12345678901234", label="branch1", status="merged"
                ),
            ]
        )
        text = await self._call("list merged", facade=facade)
        assert "Branches" in text
        assert "merged" in text

    async def test_merge_missing_id(self):
        facade = MagicMock()
        text = await self._call("merge", facade=facade)
        assert "Usage" in text

    async def test_merge_success(self):
        facade = MagicMock()
        full_id = "abcdef1234567890"
        facade.conv_branches.list = AsyncMock(
            return_value=[
                FakeBranch(branch_id=full_id, label="my-branch"),
            ]
        )
        facade.conv_branches.merge = AsyncMock(
            return_value=FakeBranch(
                branch_id=full_id, label="my-branch", status="merged"
            ),
        )
        text = await self._call(f"merge {full_id}", facade=facade)
        assert "merged" in text

    async def test_merge_not_found(self):
        facade = MagicMock()
        full_id = "abcdef1234567890"
        facade.conv_branches.list = AsyncMock(
            return_value=[
                FakeBranch(branch_id=full_id, label="my-branch"),
            ]
        )
        facade.conv_branches.merge = AsyncMock(return_value=None)
        text = await self._call(f"merge {full_id}", facade=facade)
        assert "not found" in text

    async def test_discard_missing_id(self):
        facade = MagicMock()
        text = await self._call("discard", facade=facade)
        assert "Usage" in text

    async def test_discard_success(self):
        facade = MagicMock()
        full_id = "abcdef1234567890"
        facade.conv_branches.list = AsyncMock(
            return_value=[
                FakeBranch(branch_id=full_id, label="my-branch"),
            ]
        )
        facade.conv_branches.discard = AsyncMock(
            return_value=FakeBranch(
                branch_id=full_id, label="my-branch", status="discarded"
            ),
        )
        text = await self._call(f"discard {full_id}", facade=facade)
        assert "discarded" in text

    async def test_discard_not_found(self):
        facade = MagicMock()
        full_id = "abcdef1234567890"
        facade.conv_branches.list = AsyncMock(
            return_value=[
                FakeBranch(branch_id=full_id, label="my-branch"),
            ]
        )
        facade.conv_branches.discard = AsyncMock(return_value=None)
        text = await self._call(f"discard {full_id}", facade=facade)
        assert "not found" in text

    async def test_link_git_missing_args(self):
        facade = MagicMock()
        text = await self._call("link-git", facade=facade)
        assert "Usage" in text

    async def test_link_git_missing_git_branch(self):
        facade = MagicMock()
        text = await self._call("link-git someid", facade=facade)
        assert "Usage" in text

    async def test_link_git_success(self):
        facade = MagicMock()
        full_id = "abcdef1234567890"
        facade.conv_branches.list = AsyncMock(
            return_value=[
                FakeBranch(branch_id=full_id, label="my-branch"),
            ]
        )
        facade.conv_branches.link_git_branch = AsyncMock(return_value=True)
        text = await self._call(f"link-git {full_id} feat/test", facade=facade)
        assert "linked" in text
        assert "feat/test" in text

    async def test_link_git_not_found(self):
        facade = MagicMock()
        full_id = "abcdef1234567890"
        facade.conv_branches.list = AsyncMock(
            return_value=[
                FakeBranch(branch_id=full_id, label="my-branch"),
            ]
        )
        facade.conv_branches.link_git_branch = AsyncMock(return_value=False)
        text = await self._call(f"link-git {full_id} feat/test", facade=facade)
        assert "not found" in text

    async def test_unknown_subcmd(self):
        facade = MagicMock()
        text = await self._call("bogus", facade=facade)
        assert "Usage" in text


# ---------------------------------------------------------------------------
# !review
# ---------------------------------------------------------------------------


class TestHandleReview:
    async def _call(
        self,
        args: str,
        project: str | None = "proj",
        facade: Any = None,
    ) -> str:
        send = _make_send()
        await handle_review(args, project=project, facade=facade, send=send)
        return _sent_text(send)

    async def test_no_project(self):
        text = await self._call("", project=None)
        assert "프로젝트를 먼저" in text

    async def test_no_facade(self):
        text = await self._call("", project="proj", facade=None)
        assert "unavailable" in text

    async def test_pending_empty(self):
        facade = MagicMock()
        facade.reviews.list = AsyncMock(return_value=[])
        text = await self._call("", facade=facade)
        assert "대기 중인 리뷰가 없습니다" in text

    async def test_pending_reviews(self):
        facade = MagicMock()
        facade.reviews.list = AsyncMock(
            return_value=[
                FakeReview(
                    review_id="rev1234567890abcd", artifact_id="art1234567890abcd"
                ),
            ]
        )
        text = await self._call("", facade=facade)
        assert "Pending reviews" in text

    async def test_list_invalid_status(self):
        facade = MagicMock()
        text = await self._call("list invalid", facade=facade)
        assert "Unknown status" in text

    async def test_list_valid_statuses(self):
        for s in ("pending", "approved", "rejected"):
            facade = MagicMock()
            facade.reviews.list = AsyncMock(return_value=[])
            text = await self._call(f"list {s}", facade=facade)
            assert "Unknown status" not in text

    async def test_list_empty(self):
        facade = MagicMock()
        facade.reviews.list = AsyncMock(return_value=[])
        text = await self._call("list pending", facade=facade)
        assert "No reviews" in text

    async def test_list_with_reviews(self):
        facade = MagicMock()
        facade.reviews.list = AsyncMock(
            return_value=[
                FakeReview(
                    review_id="rev1234567890abcd",
                    artifact_id="art1234567890abcd",
                    status="approved",
                ),
            ]
        )
        text = await self._call("list approved", facade=facade)
        assert "Reviews" in text
        assert "approved" in text

    async def test_approve_missing_id(self):
        facade = MagicMock()
        text = await self._call("approve", facade=facade)
        assert "Usage" in text

    async def test_approve_success(self):
        facade = MagicMock()
        full_id = "rev1234567890abcd"
        facade.reviews.list = AsyncMock(
            return_value=[
                FakeReview(review_id=full_id, artifact_id="art123"),
            ]
        )
        facade.reviews.approve = AsyncMock(return_value=True)
        text = await self._call(f"approve {full_id}", facade=facade)
        assert "approved" in text

    async def test_approve_with_comment(self):
        facade = MagicMock()
        full_id = "rev1234567890abcd"
        facade.reviews.list = AsyncMock(
            return_value=[
                FakeReview(review_id=full_id, artifact_id="art123"),
            ]
        )
        facade.reviews.approve = AsyncMock(return_value=True)
        text = await self._call(f"approve {full_id} looks good", facade=facade)
        assert "approved" in text
        facade.reviews.approve.assert_awaited_once()
        call_kwargs = facade.reviews.approve.call_args.kwargs
        assert call_kwargs.get("comment") == "looks good"

    async def test_approve_not_found(self):
        facade = MagicMock()
        full_id = "rev1234567890abcd"
        facade.reviews.list = AsyncMock(
            return_value=[
                FakeReview(review_id=full_id, artifact_id="art123"),
            ]
        )
        facade.reviews.approve = AsyncMock(return_value=None)
        text = await self._call(f"approve {full_id}", facade=facade)
        assert "not found" in text

    async def test_reject_missing_id(self):
        facade = MagicMock()
        text = await self._call("reject", facade=facade)
        assert "Usage" in text

    async def test_reject_success(self):
        facade = MagicMock()
        full_id = "rev1234567890abcd"
        facade.reviews.list = AsyncMock(
            return_value=[
                FakeReview(review_id=full_id, artifact_id="art123"),
            ]
        )
        facade.reviews.reject = AsyncMock(return_value=True)
        text = await self._call(f"reject {full_id}", facade=facade)
        assert "rejected" in text

    async def test_reject_with_comment(self):
        facade = MagicMock()
        full_id = "rev1234567890abcd"
        facade.reviews.list = AsyncMock(
            return_value=[
                FakeReview(review_id=full_id, artifact_id="art123"),
            ]
        )
        facade.reviews.reject = AsyncMock(return_value=True)
        text = await self._call(f"reject {full_id} needs work", facade=facade)
        assert "rejected" in text
        call_kwargs = facade.reviews.reject.call_args.kwargs
        assert call_kwargs.get("comment") == "needs work"

    async def test_reject_not_found(self):
        facade = MagicMock()
        full_id = "rev1234567890abcd"
        facade.reviews.list = AsyncMock(
            return_value=[
                FakeReview(review_id=full_id, artifact_id="art123"),
            ]
        )
        facade.reviews.reject = AsyncMock(return_value=None)
        text = await self._call(f"reject {full_id}", facade=facade)
        assert "not found" in text

    async def test_unknown_subcmd(self):
        facade = MagicMock()
        text = await self._call("bogus", facade=facade)
        assert "Usage" in text


# ---------------------------------------------------------------------------
# !context
# ---------------------------------------------------------------------------


class TestHandleContext:
    async def test_no_project(self):
        send = _make_send()
        await handle_context(project=None, facade=None, send=send)
        assert "프로젝트를 먼저" in _sent_text(send)

    async def test_no_facade(self):
        send = _make_send()
        await handle_context(project="proj", facade=None, send=send)
        assert "unavailable" in _sent_text(send)

    async def test_empty_context(self):
        send = _make_send()
        facade = MagicMock()
        facade.get_project_context = AsyncMock(return_value="")
        await handle_context(project="proj", facade=facade, send=send)
        assert "컨텍스트가 없습니다" in _sent_text(send)

    async def test_with_context(self):
        send = _make_send()
        facade = MagicMock()
        facade.get_project_context = AsyncMock(return_value="full context here")
        await handle_context(project="proj", facade=facade, send=send)
        assert _sent_text(send) == "full context here"

    async def test_none_context(self):
        send = _make_send()
        facade = MagicMock()
        facade.get_project_context = AsyncMock(return_value=None)
        await handle_context(project="proj", facade=facade, send=send)
        assert "컨텍스트가 없습니다" in _sent_text(send)


# ---------------------------------------------------------------------------
# !rt
# ---------------------------------------------------------------------------


class TestHandleRt:
    async def test_no_engines(self):
        send = _make_send()
        runtime = _make_runtime(engines=[])
        runtime.roundtable.engines = []
        await handle_rt(
            "some topic", runtime=runtime, send=send, start_roundtable=AsyncMock()
        )
        text = _sent_text(send)
        assert "No engines available" in text

    async def test_empty_args_shows_usage(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude", "codex"])
        runtime.roundtable.engines = ["claude", "codex"]
        await handle_rt("", runtime=runtime, send=send, start_roundtable=AsyncMock())
        text = _sent_text(send)
        assert "Roundtable" in text
        assert "`claude`" in text

    async def test_fallback_engines(self):
        send = _make_send()
        runtime = _make_runtime(engines=["gemini"])
        runtime.roundtable.engines = []
        await handle_rt("", runtime=runtime, send=send, start_roundtable=AsyncMock())
        text = _sent_text(send)
        assert "`gemini`" in text

    async def test_start_roundtable(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude", "codex"])
        runtime.roundtable.engines = ["claude", "codex"]
        start_rt = AsyncMock()
        await handle_rt(
            '"Design a new API"',
            runtime=runtime,
            send=send,
            start_roundtable=start_rt,
        )
        start_rt.assert_awaited_once()

    async def test_close_without_callback(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude"])
        runtime.roundtable.engines = ["claude"]
        await handle_rt(
            "close",
            runtime=runtime,
            send=send,
            start_roundtable=AsyncMock(),
            close_roundtable=None,
        )
        text = _sent_text(send)
        assert "can only be used inside" in text

    async def test_close_with_callback(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude"])
        runtime.roundtable.engines = ["claude"]
        close_rt = AsyncMock()
        await handle_rt(
            "close",
            runtime=runtime,
            send=send,
            start_roundtable=AsyncMock(),
            close_roundtable=close_rt,
        )
        close_rt.assert_awaited_once()

    async def test_follow_without_callback(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude"])
        runtime.roundtable.engines = ["claude"]
        await handle_rt(
            'follow "what about X?"',
            runtime=runtime,
            send=send,
            start_roundtable=AsyncMock(),
            continue_roundtable=None,
        )
        text = _sent_text(send)
        assert "can only be used inside" in text

    async def test_follow_empty_shows_usage(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude"])
        runtime.roundtable.engines = ["claude"]
        continue_rt = AsyncMock()
        await handle_rt(
            "follow",
            runtime=runtime,
            send=send,
            start_roundtable=AsyncMock(),
            continue_roundtable=continue_rt,
        )
        text = _sent_text(send)
        assert "Follow-up" in text

    async def test_follow_with_topic(self):
        send = _make_send()
        runtime = _make_runtime(engines=["claude"])
        runtime.roundtable.engines = ["claude"]
        continue_rt = AsyncMock()
        await handle_rt(
            'follow "what next?"',
            runtime=runtime,
            send=send,
            start_roundtable=AsyncMock(),
            continue_roundtable=continue_rt,
        )
        continue_rt.assert_awaited_once()


# ---------------------------------------------------------------------------
# !cancel
# ---------------------------------------------------------------------------


class TestHandleCancel:
    async def test_no_running_task(self):
        send = _make_send()
        await handle_cancel(channel_id="ch1", running_tasks={}, send=send)
        text = _sent_text(send)
        assert "No running task" in text

    async def test_cancel_matching_task(self):
        from tunapi.transport import MessageRef

        ref = MessageRef(channel_id="ch1", message_id="msg1")
        cancel_event = MagicMock()
        task = MagicMock()
        task.cancel_requested = cancel_event
        send = _make_send()
        await handle_cancel(channel_id="ch1", running_tasks={ref: task}, send=send)
        cancel_event.set.assert_called_once()
        assert "cancelled" in _sent_text(send)

    async def test_cancel_different_channel(self):
        from tunapi.transport import MessageRef

        ref = MessageRef(channel_id="other_ch", message_id="msg1")
        task = MagicMock()
        task.cancel_requested = MagicMock()
        send = _make_send()
        await handle_cancel(channel_id="ch1", running_tasks={ref: task}, send=send)
        assert "No running task" in _sent_text(send)

    async def test_cancel_only_first_match(self):
        from tunapi.transport import MessageRef

        ref1 = MessageRef(channel_id="ch1", message_id="msg1")
        ref2 = MessageRef(channel_id="ch1", message_id="msg2")
        task1 = MagicMock()
        task1.cancel_requested = MagicMock()
        task2 = MagicMock()
        task2.cancel_requested = MagicMock()
        send = _make_send()
        await handle_cancel(
            channel_id="ch1", running_tasks={ref1: task1, ref2: task2}, send=send
        )
        # At least one should be cancelled, total cancel calls = 1
        total = (
            task1.cancel_requested.set.call_count
            + task2.cancel_requested.set.call_count
        )
        assert total == 1
        assert "cancelled" in _sent_text(send)


# ---------------------------------------------------------------------------
# _resolve_id
# ---------------------------------------------------------------------------


class TestResolveId:
    async def test_prefix_too_short(self):
        result_id, err = await _resolve_id(
            "abc",
            fetch_all=AsyncMock(return_value=[]),
            get_id=lambda x: x,
            get_label=lambda x: x,
        )
        assert result_id is None
        assert "too short" in err

    async def test_exact_match(self):
        items = ["abcdef123456"]
        result_id, err = await _resolve_id(
            "abcdef123456",
            fetch_all=AsyncMock(return_value=items),
            get_id=lambda x: x,
            get_label=lambda x: x,
        )
        assert result_id == "abcdef123456"
        assert err is None

    async def test_unique_prefix_match(self):
        items = ["abcdef123456", "xyz789000000"]
        result_id, err = await _resolve_id(
            "abcdef",
            fetch_all=AsyncMock(return_value=items),
            get_id=lambda x: x,
            get_label=lambda x: x,
        )
        assert result_id == "abcdef123456"
        assert err is None

    async def test_no_match(self):
        items = ["abcdef123456"]
        result_id, err = await _resolve_id(
            "zzzzzzz",
            fetch_all=AsyncMock(return_value=items),
            get_id=lambda x: x,
            get_label=lambda x: x,
        )
        assert result_id is None
        assert "not found" in err

    async def test_ambiguous_match(self):
        items = ["abcdef111111", "abcdef222222", "abcdef333333"]
        result_id, err = await _resolve_id(
            "abcdef",
            fetch_all=AsyncMock(return_value=items),
            get_id=lambda x: x,
            get_label=lambda x: x,
        )
        assert result_id is None
        assert "Ambiguous" in err
        assert "3 matches" in err

    async def test_ambiguous_truncates_at_5(self):
        items = [f"abcdef{i:06d}" for i in range(8)]
        result_id, err = await _resolve_id(
            "abcdef",
            fetch_all=AsyncMock(return_value=items),
            get_id=lambda x: x,
            get_label=lambda x: x,
        )
        assert result_id is None
        assert "... and 3 more" in err

    async def test_min_prefix_len_constant(self):
        assert _MIN_PREFIX_LEN == 6

    async def test_boundary_prefix_length(self):
        # Exactly _MIN_PREFIX_LEN chars should work
        items = ["abcdef123456"]
        result_id, err = await _resolve_id(
            "abcdef",
            fetch_all=AsyncMock(return_value=items),
            get_id=lambda x: x,
            get_label=lambda x: x,
        )
        assert result_id == "abcdef123456"
        assert err is None

    async def test_5_char_prefix_too_short(self):
        result_id, err = await _resolve_id(
            "abcde",
            fetch_all=AsyncMock(return_value=[]),
            get_id=lambda x: x,
            get_label=lambda x: x,
        )
        assert result_id is None
        assert "too short" in err
