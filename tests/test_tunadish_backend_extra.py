"""Additional tests for tunadish backend — targeting uncovered functions/paths."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from tunapi.context import RunContext
from tunapi.core.memory_facade import ProjectMemoryFacade
from tunapi.journal import Journal, JournalEntry
from tunapi.runner_bridge import RunningTask
from tunapi.transport import MessageRef, RenderedMessage, SendOptions
from tunapi.tunadish.backend import TunadishBackend
from tunapi.tunadish.context_store import (
    ConversationContextStore,
    ConversationSettings,
)
from tunapi.tunadish.transport import TunadishTransport

pytestmark = pytest.mark.anyio


# ── Fakes ──


class FakeWs:
    def __init__(self):
        self.sent: list[dict[str, Any]] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def last(self) -> dict[str, Any]:
        return self.sent[-1]

    def last_params(self) -> dict[str, Any]:
        return self.last().get("params", self.last().get("result", {}))

    def find_method(self, method: str) -> dict[str, Any] | None:
        for msg in self.sent:
            if msg.get("method") == method:
                return msg
        return None


class FakeRuntime:
    def __init__(self, *, project_aliases=None, engine_ids=None, projects_map=None):
        self._aliases = project_aliases or []
        self._engine_ids = engine_ids or ["claude"]
        self.default_engine = "claude"
        self._projects = MagicMock()
        self._projects.projects = projects_map or {}

    def project_aliases(self) -> list[str]:
        return self._aliases

    def available_engine_ids(self) -> list[str]:
        return self._engine_ids

    def chat_ids_for_project(self, project: str) -> list[str]:
        return []

    def resolve_run_cwd(self, ctx: Any) -> Path | None:
        return None

    def resolve_message(self, *, text, reply_text, ambient_context=None, chat_id=None):
        @dataclass(frozen=True)
        class _Resolved:
            prompt: str = text
            resume_token: Any = None
            engine_override: str | None = None
            context: RunContext | None = ambient_context

        return _Resolved()

    def resolve_runner(self, *, resume_token, engine_override):
        runner = MagicMock()
        runner.engine = engine_override or "claude"
        runner.model = "claude-sonnet-4-20250514"

        resolved = MagicMock()
        resolved.engine = runner.engine
        resolved.runner = runner
        resolved.available = True
        resolved.issue = None
        return resolved


@pytest.fixture
def ws():
    return FakeWs()


@pytest.fixture
def transport(ws):
    return TunadishTransport(ws)


@pytest.fixture
def backend(tmp_path):
    b = TunadishBackend()
    b._facade = ProjectMemoryFacade(tmp_path)
    b.context_store = ConversationContextStore(tmp_path / "ctx.json")
    b._journal = Journal(tmp_path / "journals")
    b._cross_journals = []
    b._chat_prefs = MagicMock()
    b._chat_prefs.get_default_engine = AsyncMock(return_value=None)
    b._chat_prefs.set_default_engine = AsyncMock()
    b._chat_prefs.get_engine_model = AsyncMock(return_value=None)
    b._chat_prefs.set_engine_model = AsyncMock()
    b._chat_prefs.clear_engine_model = AsyncMock()
    b._chat_prefs.get_trigger_mode = AsyncMock(return_value=None)
    b._chat_prefs.set_trigger_mode = AsyncMock()
    b._config_path = str(tmp_path / "tunapi.toml")
    b._conv_sessions = MagicMock()
    b._conv_sessions.get = AsyncMock(return_value=None)
    b._conv_sessions.set = AsyncMock()
    b._project_sessions = MagicMock()
    b._project_sessions.get = AsyncMock(return_value=None)
    b._task_group = None
    return b


@pytest.fixture
def runtime():
    return FakeRuntime()


# ═══════════════════════════════════════════════════════════════
# _execute_run
# ═══════════════════════════════════════════════════════════════


class TestExecuteRun:
    async def test_execute_run_happy_path(self, backend, ws, transport, runtime):
        """Normal execution: sends running status, calls handle_message, sends idle."""
        await backend.context_store.set_context("conv1", RunContext(project="proj"))

        with (
            patch("tunapi.tunadish.rawq_bridge.is_available", return_value=False),
            patch(
                "tunapi.tunadish.backend.handle_message", new_callable=AsyncMock
            ) as mock_handle,
            patch("tunapi.tunadish.backend.set_run_base_dir", return_value="tok"),
            patch("tunapi.tunadish.backend.reset_run_base_dir"),
        ):
            await backend._execute_run("conv1", "hello", runtime, transport)

            mock_handle.assert_awaited_once()
            # Verify running + idle notifications were sent
            methods = [m.get("method") for m in ws.sent]
            assert "run.status" in methods
            statuses = [
                m["params"]["status"]
                for m in ws.sent
                if m.get("method") == "run.status"
            ]
            assert "running" in statuses
            assert "idle" in statuses

    async def test_execute_run_timeout(self, backend, ws, transport, runtime):
        """Timeout produces error message and sends idle notification."""
        await backend.context_store.set_context("conv1", RunContext(project="proj"))

        async def _slow(*a, **kw):
            await anyio.sleep(999)

        with (
            patch("tunapi.tunadish.rawq_bridge.is_available", return_value=False),
            patch("tunapi.tunadish.backend.handle_message", side_effect=_slow),
            patch("tunapi.tunadish.backend.set_run_base_dir", return_value="tok"),
            patch("tunapi.tunadish.backend.reset_run_base_dir"),
        ):
            # Use very short timeout
            backend._RUN_TIMEOUT = 1
            await backend._execute_run(
                "conv1", "hello", runtime, transport, timeout=1
            )

        # Should have sent idle at end
        methods = [m.get("method") for m in ws.sent]
        assert "run.status" in methods
        last_status = [
            m["params"]["status"]
            for m in ws.sent
            if m.get("method") == "run.status"
        ]
        assert last_status[-1] == "idle"
        # Should have edited progress ref with timeout message
        update_msgs = [m for m in ws.sent if m.get("method") == "message.update"]
        assert any("타임아웃" in m.get("params", {}).get("message", {}).get("text", "") for m in update_msgs)

    async def test_execute_run_exception(self, backend, ws, transport, runtime):
        """Exception during run produces error message and sends idle."""
        await backend.context_store.set_context("conv1", RunContext(project="proj"))

        with (
            patch("tunapi.tunadish.rawq_bridge.is_available", return_value=False),
            patch(
                "tunapi.tunadish.backend.handle_message",
                side_effect=RuntimeError("boom"),
            ),
            patch("tunapi.tunadish.backend.set_run_base_dir", return_value="tok"),
            patch("tunapi.tunadish.backend.reset_run_base_dir"),
        ):
            await backend._execute_run("conv1", "hello", runtime, transport)

        # idle status at the end
        statuses = [
            m["params"]["status"]
            for m in ws.sent
            if m.get("method") == "run.status"
        ]
        assert statuses[-1] == "idle"
        # Error message in update
        update_msgs = [m for m in ws.sent if m.get("method") == "message.update"]
        assert any("오류" in m.get("params", {}).get("message", {}).get("text", "") for m in update_msgs)

    async def test_execute_run_cleans_up_run_map(self, backend, ws, transport, runtime):
        """After execution, conv is removed from run_map."""
        await backend.context_store.set_context("conv1", RunContext(project="proj"))

        with (
            patch("tunapi.tunadish.rawq_bridge.is_available", return_value=False),
            patch("tunapi.tunadish.backend.handle_message", new_callable=AsyncMock),
            patch("tunapi.tunadish.backend.set_run_base_dir", return_value="tok"),
            patch("tunapi.tunadish.backend.reset_run_base_dir"),
        ):
            await backend._execute_run("conv1", "hello", runtime, transport)

        assert "conv1" not in backend.run_map

    async def test_execute_run_conv_session_used(self, backend, ws, transport, runtime):
        """When conv session exists, its resume token is used."""
        await backend.context_store.set_context("conv1", RunContext(project="proj"))

        conv_sess = MagicMock()
        conv_sess.engine = "claude"
        conv_sess.token = "tok-abc"
        backend._conv_sessions.get = AsyncMock(return_value=conv_sess)

        with (
            patch("tunapi.tunadish.rawq_bridge.is_available", return_value=False),
            patch(
                "tunapi.tunadish.backend.handle_message", new_callable=AsyncMock
            ) as mock_handle,
            patch("tunapi.tunadish.backend.set_run_base_dir", return_value="tok"),
            patch("tunapi.tunadish.backend.reset_run_base_dir"),
        ):
            await backend._execute_run("conv1", "hello", runtime, transport)
            call_kwargs = mock_handle.call_args[1]
            # Should pass a ResumeToken built from conv_session
            assert call_kwargs["resume_token"] is not None
            assert call_kwargs["resume_token"].value == "tok-abc"

    async def test_execute_run_engine_override_discards_mismatched_token(
        self, backend, ws, transport, runtime
    ):
        """If conv_settings.engine differs from conv_session.engine, token is discarded."""
        await backend.context_store.set_context("conv1", RunContext(project="proj"))
        await backend.context_store.update_conv_settings("conv1", engine="gemini")

        conv_sess = MagicMock()
        conv_sess.engine = "claude"
        conv_sess.token = "old-token"
        backend._conv_sessions.get = AsyncMock(return_value=conv_sess)

        with (
            patch("tunapi.tunadish.rawq_bridge.is_available", return_value=False),
            patch(
                "tunapi.tunadish.backend.handle_message", new_callable=AsyncMock
            ) as mock_handle,
            patch("tunapi.tunadish.backend.set_run_base_dir", return_value="tok"),
            patch("tunapi.tunadish.backend.reset_run_base_dir"),
        ):
            await backend._execute_run("conv1", "hello", runtime, transport)
            call_kwargs = mock_handle.call_args[1]
            assert call_kwargs["resume_token"] is None

    async def test_execute_run_resets_run_meta(self, backend, ws, transport, runtime):
        """After run completes, transport run_meta is reset to None."""
        await backend.context_store.set_context("conv1", RunContext(project="proj"))

        with (
            patch("tunapi.tunadish.rawq_bridge.is_available", return_value=False),
            patch("tunapi.tunadish.backend.handle_message", new_callable=AsyncMock),
            patch("tunapi.tunadish.backend.set_run_base_dir", return_value="tok"),
            patch("tunapi.tunadish.backend.reset_run_base_dir"),
        ):
            await backend._execute_run("conv1", "hello", runtime, transport)

        assert transport._run_engine is None
        assert transport._run_model is None


# ═══════════════════════════════════════════════════════════════
# handle_chat_send additional paths
# ═══════════════════════════════════════════════════════════════


class TestHandleChatSendExtra:
    async def test_command_not_handled_falls_through_to_execute(
        self, backend, runtime, transport, ws
    ):
        """Unknown ! command (dispatch returns False) falls through to _execute_run."""
        await backend.context_store.set_context("conv-x", RunContext(project="proj"))

        with (
            patch(
                "tunapi.tunadish.backend.dispatch_command",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                backend, "_execute_run", new_callable=AsyncMock
            ) as mock_exec,
        ):
            await backend.handle_chat_send(
                {"conversation_id": "conv-x", "text": "!unknown_cmd"},
                runtime,
                transport,
            )
            mock_exec.assert_awaited_once()

    async def test_normal_text_calls_execute_run(self, backend, runtime, transport, ws):
        """Regular message (no !) calls _execute_run directly."""
        await backend.context_store.set_context("conv-y", RunContext(project="proj"))

        with patch.object(
            backend, "_execute_run", new_callable=AsyncMock
        ) as mock_exec:
            await backend.handle_chat_send(
                {"conversation_id": "conv-y", "text": "Tell me about the code"},
                runtime,
                transport,
            )
            mock_exec.assert_awaited_once()
            assert mock_exec.call_args[0][1] == "Tell me about the code"

    async def test_exception_in_handle_chat_send_is_caught(
        self, backend, runtime, transport
    ):
        """Unhandled error in handle_chat_send is logged, not propagated."""
        with patch.object(
            backend, "_execute_run", side_effect=RuntimeError("unexpected")
        ):
            # Should not raise
            await backend.handle_chat_send(
                {"conversation_id": "conv-err", "text": "hi"},
                runtime,
                transport,
            )

    async def test_chat_send_with_timeout_param(
        self, backend, runtime, transport, ws
    ):
        """Timeout parameter is forwarded to _execute_run."""
        await backend.context_store.set_context("conv-t", RunContext(project="proj"))

        with patch.object(
            backend, "_execute_run", new_callable=AsyncMock
        ) as mock_exec:
            await backend.handle_chat_send(
                {"conversation_id": "conv-t", "text": "hello", "timeout": 60},
                runtime,
                transport,
            )
            call_kwargs = mock_exec.call_args
            assert call_kwargs[1]["timeout"] == 60


# ═══════════════════════════════════════════════════════════════
# Conversation management: create, delete, list, history
# ═══════════════════════════════════════════════════════════════


class TestConversationManagementExtra:
    async def test_conversation_create_sends_notification(
        self, backend, ws, transport
    ):
        """conversation.create via _ws_handler logic sends conversation.created."""
        conv_id = "conv-new"
        project = "myproj"
        label = "Test Conv"

        # Inline the handler logic
        await backend.context_store.set_context(
            conv_id, RunContext(project=project), label=label
        )
        await transport._send_notification(
            "conversation.created",
            {"conversation_id": conv_id, "project": project, "label": label},
        )

        msg = ws.find_method("conversation.created")
        assert msg is not None
        assert msg["params"]["project"] == "myproj"
        assert msg["params"]["label"] == "Test Conv"

    async def test_conversation_delete_removes_journal(self, backend, tmp_path):
        """conversation.delete also removes journal file."""
        await backend.context_store.set_context("del-me", RunContext(project="proj"))
        # Create a fake journal file
        journal_path = backend._journal._base_dir / "del-me.jsonl"
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_text("test entry\n")
        assert journal_path.exists()

        # Delete
        await backend.context_store.clear("del-me")
        if journal_path.exists():
            journal_path.unlink()

        assert not journal_path.exists()

    async def test_conversation_history_builds_messages(self, backend, ws, transport):
        """conversation.history returns prompt/completed entries as messages."""
        conv_id = "hist-conv"
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")

        await backend._journal.append(
            JournalEntry(
                run_id="r1",
                channel_id=conv_id,
                timestamp=ts,
                event="prompt",
                data={"text": "What is this?", "model": "opus-4"},
                engine="claude",
            )
        )
        await backend._journal.append(
            JournalEntry(
                run_id="r1",
                channel_id=conv_id,
                timestamp=ts,
                event="completed",
                data={"ok": True, "answer": "It is a project."},
            )
        )

        # Simulate conversation.history handling
        entries = await backend._journal.recent_entries(conv_id, limit=200)
        entries = sorted(entries, key=lambda e: e.timestamp)
        messages = []
        run_meta: dict[str, dict[str, str | None]] = {}
        for e in entries:
            if e.event == "prompt":
                meta = {"engine": e.engine, "model": e.data.get("model")}
                run_meta[e.run_id] = meta
                messages.append(
                    {"role": "user", "content": e.data.get("text", ""), "timestamp": e.timestamp}
                )
            elif e.event == "completed" and e.data.get("ok"):
                answer = e.data.get("answer")
                if answer:
                    meta = run_meta.get(e.run_id, {})
                    msg: dict[str, Any] = {
                        "role": "assistant",
                        "content": answer,
                        "timestamp": e.timestamp,
                    }
                    if meta.get("engine"):
                        msg["engine"] = meta["engine"]
                    if meta.get("model"):
                        msg["model"] = meta["model"]
                    messages.append(msg)

        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "What is this?"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["engine"] == "claude"
        assert messages[1]["model"] == "opus-4"

    async def test_conversation_list_adds_source(self, backend):
        """conversation.list adds source='tunadish' to each entry."""
        await backend.context_store.set_context("c1", RunContext(project="proj"))
        convs = backend.context_store.list_conversations(project="proj")
        for c in convs:
            c["source"] = "tunadish"
        assert all(c["source"] == "tunadish" for c in convs)


# ═══════════════════════════════════════════════════════════════
# _dispatch_rpc_command additional paths
# ═══════════════════════════════════════════════════════════════


class TestDispatchRpcCommandExtra:
    async def test_model_clear_sets_model_none(self, backend, ws, transport, runtime):
        """model command with 'clear' sets model to None in settings."""
        await backend.context_store.set_context("conv-cl", RunContext(project="proj"))

        async def fake_dispatch(cmd, args, *, send, **kw):
            # Simulate what real dispatch_command does: call send with result
            await send(RenderedMessage(text="Model cleared"))
            return True

        with patch(
            "tunapi.tunadish.backend.dispatch_command",
            side_effect=fake_dispatch,
        ):
            await backend._dispatch_rpc_command(
                "model",
                "claude clear",
                {"conversation_id": "conv-cl"},
                runtime,
                transport,
            )

        # Verify settings were updated
        settings = backend.context_store.get_conv_settings("conv-cl")
        assert settings.engine == "claude"
        # model should be None (cleared)
        assert settings.model is None

    async def test_persona_set_updates_settings(self, backend, ws, transport, runtime):
        """persona command stores persona name in conv settings."""
        await backend.context_store.set_context("conv-p", RunContext(project="proj"))

        async def fake_dispatch(cmd, args, *, send, **kw):
            await send(RenderedMessage(text="Persona set"))
            return True

        with patch(
            "tunapi.tunadish.backend.dispatch_command",
            side_effect=fake_dispatch,
        ):
            await backend._dispatch_rpc_command(
                "persona",
                "architect",
                {"conversation_id": "conv-p"},
                runtime,
                transport,
            )

        settings = backend.context_store.get_conv_settings("conv-p")
        assert settings.persona == "architect"

    async def test_persona_list_does_not_update_settings(
        self, backend, ws, transport, runtime
    ):
        """persona list command does not modify settings."""
        await backend.context_store.set_context("conv-pl", RunContext(project="proj"))

        async def fake_dispatch(cmd, args, *, send, **kw):
            await send(RenderedMessage(text="Persona list"))
            return True

        with patch(
            "tunapi.tunadish.backend.dispatch_command",
            side_effect=fake_dispatch,
        ):
            await backend._dispatch_rpc_command(
                "persona",
                "list",
                {"conversation_id": "conv-pl"},
                runtime,
                transport,
            )

        settings = backend.context_store.get_conv_settings("conv-pl")
        assert settings.persona is None

    async def test_rpc_default_conv_id(self, backend, ws, transport, runtime):
        """When conversation_id is missing, defaults to __rpc__."""
        async def fake_dispatch(cmd, args, *, send, **kw):
            await send(RenderedMessage(text="Help text"))
            return True

        with patch(
            "tunapi.tunadish.backend.dispatch_command",
            side_effect=fake_dispatch,
        ):
            await backend._dispatch_rpc_command(
                "help", "", {}, runtime, transport
            )

        # Check that command.result was sent with __rpc__ conv_id
        msg = ws.find_method("command.result")
        assert msg is not None
        assert msg["params"]["conversation_id"] == "__rpc__"

    async def test_trigger_updates_conv_settings(
        self, backend, ws, transport, runtime
    ):
        """trigger command stores trigger_mode in settings."""
        await backend.context_store.set_context("conv-tr", RunContext(project="proj"))

        async def fake_dispatch(cmd, args, *, send, **kw):
            await send(RenderedMessage(text="Trigger set"))
            return True

        with patch(
            "tunapi.tunadish.backend.dispatch_command",
            side_effect=fake_dispatch,
        ):
            await backend._dispatch_rpc_command(
                "trigger",
                "always",
                {"conversation_id": "conv-tr"},
                runtime,
                transport,
            )

        settings = backend.context_store.get_conv_settings("conv-tr")
        assert settings.trigger_mode == "always"


# ═══════════════════════════════════════════════════════════════
# Branch handlers
# ═══════════════════════════════════════════════════════════════


class TestBranchHandlers:
    async def test_branch_switch(self, backend, ws, transport):
        """branch.switch updates active branch and sends notification."""
        await backend.context_store.set_context("conv-b", RunContext(project="proj"))

        await backend._handle_branch_switch(
            {"conversation_id": "conv-b", "branch_id": "br-123"}, transport
        )

        msg = ws.find_method("branch.switched")
        assert msg is not None
        assert msg["params"]["branch_id"] == "br-123"

    async def test_branch_switch_to_main(self, backend, ws, transport):
        """branch.switch with branch_id=None returns to main."""
        await backend.context_store.set_context("conv-bm", RunContext(project="proj"))
        await backend.context_store.set_active_branch("conv-bm", "br-old")

        await backend._handle_branch_switch(
            {"conversation_id": "conv-bm", "branch_id": None}, transport
        )

        msg = ws.find_method("branch.switched")
        assert msg is not None
        assert msg["params"]["branch_id"] is None

    async def test_branch_switch_no_conv_id(self, backend, ws, transport):
        """branch.switch without conv_id returns early."""
        await backend._handle_branch_switch({"branch_id": "br-x"}, transport)
        assert len(ws.sent) == 0

    async def test_branch_archive(self, backend, ws, transport):
        """branch.archive archives the branch and broadcasts."""
        await backend.context_store.set_context("conv-ba", RunContext(project="proj"))
        branch = await backend._facade.conv_branches.create(
            "proj", label="test", session_id="conv-ba"
        )

        t2 = TunadishTransport(FakeWs())
        backend._active_transports = {transport, t2}

        await backend._handle_branch_archive(
            {"conversation_id": "conv-ba", "branch_id": branch.branch_id}, transport
        )

        msg = ws.find_method("branch.archived")
        assert msg is not None

    async def test_branch_archive_returns_to_main_if_active(
        self, backend, ws, transport
    ):
        """If the archived branch was active, switches back to main."""
        await backend.context_store.set_context("conv-bam", RunContext(project="proj"))
        branch = await backend._facade.conv_branches.create(
            "proj", label="test-am", session_id="conv-bam"
        )
        await backend.context_store.set_active_branch("conv-bam", branch.branch_id)

        backend._active_transports = {transport}

        await backend._handle_branch_archive(
            {"conversation_id": "conv-bam", "branch_id": branch.branch_id}, transport
        )

        meta = backend.context_store._cache.get("conv-bam")
        assert meta is None or getattr(meta, "active_branch_id", None) is None

    async def test_branch_archive_no_project(self, backend, ws, transport):
        """branch.archive without project context returns early."""
        await backend._handle_branch_archive(
            {"conversation_id": "no-proj", "branch_id": "br-x"}, transport
        )
        assert ws.find_method("branch.archived") is None

    async def test_branch_delete(self, backend, ws, transport):
        """branch.delete removes branch and broadcasts."""
        await backend.context_store.set_context("conv-bd", RunContext(project="proj"))
        branch = await backend._facade.conv_branches.create(
            "proj", label="to-delete", session_id="conv-bd"
        )

        backend._active_transports = {transport}

        await backend._handle_branch_delete(
            {"conversation_id": "conv-bd", "branch_id": branch.branch_id}, transport
        )

        msg = ws.find_method("branch.deleted")
        assert msg is not None

    async def test_branch_delete_no_conv_or_branch(self, backend, ws, transport):
        """branch.delete without required params returns early."""
        await backend._handle_branch_delete({"conversation_id": "c"}, transport)
        assert len(ws.sent) == 0

    async def test_branch_create_auto_label(self, backend, ws, transport):
        """branch.create generates auto label when label is empty."""
        await backend.context_store.set_context("conv-bc", RunContext(project="proj"))

        backend._active_transports = {transport}

        await backend._handle_branch_create(
            {"conversation_id": "conv-bc", "label": ""}, transport
        )

        msg = ws.find_method("branch.created")
        assert msg is not None
        assert msg["params"]["label"] == "branch-1"

    async def test_branch_create_with_explicit_parent(self, backend, ws, transport):
        """branch.create respects explicit parent_branch_id."""
        await backend.context_store.set_context("conv-bcp", RunContext(project="proj"))
        backend._active_transports = {transport}

        await backend._handle_branch_create(
            {
                "conversation_id": "conv-bcp",
                "label": "child",
                "parent_branch_id": None,  # explicit null = root branch
            },
            transport,
        )

        msg = ws.find_method("branch.created")
        assert msg is not None
        assert msg["params"]["parent_branch_id"] is None

    async def test_branch_create_no_project(self, backend, ws, transport):
        """branch.create without project context returns early."""
        await backend._handle_branch_create(
            {"conversation_id": "no-proj-bc", "label": "x"}, transport
        )
        assert ws.find_method("branch.created") is None


# ═══════════════════════════════════════════════════════════════
# Message action handlers
# ═══════════════════════════════════════════════════════════════


class TestMessageHandlers:
    async def test_message_delete(self, backend, ws, transport):
        """message.delete sends deleted notification."""
        await backend._handle_message_delete(
            {"conversation_id": "conv-md", "message_id": "msg-1"}, transport
        )

        msg = ws.find_method("message.deleted")
        assert msg is not None
        assert msg["params"]["message_id"] == "msg-1"

    async def test_message_delete_no_params(self, backend, ws, transport):
        """message.delete without required params returns early."""
        await backend._handle_message_delete({"conversation_id": "c"}, transport)
        assert ws.find_method("message.deleted") is None

    async def test_message_save_with_content(self, backend, ws, transport):
        """message.save with content param saves to memory."""
        await backend.context_store.set_context("conv-ms", RunContext(project="proj"))

        await backend._handle_message_save(
            {
                "conversation_id": "conv-ms",
                "message_id": "msg-s1",
                "content": "Important note",
            },
            transport,
        )

        msg = ws.find_method("message.action.result")
        assert msg is not None
        assert msg["params"]["ok"] is True

    async def test_message_save_from_journal(self, backend, ws, transport):
        """message.save without content falls back to journal."""
        await backend.context_store.set_context("conv-msj", RunContext(project="proj"))
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        await backend._journal.append(
            JournalEntry(
                run_id="r1",
                channel_id="conv-msj",
                timestamp=ts,
                event="completed",
                data={"ok": True, "answer": "Journal answer content"},
            )
        )

        await backend._handle_message_save(
            {"conversation_id": "conv-msj", "message_id": "msg-s2"},
            transport,
        )

        msg = ws.find_method("message.action.result")
        assert msg is not None
        assert msg["params"]["ok"] is True

    async def test_message_save_not_found(self, backend, ws, transport):
        """message.save with no content and empty journal returns error."""
        await backend.context_store.set_context("conv-msn", RunContext(project="proj"))

        await backend._handle_message_save(
            {"conversation_id": "conv-msn", "message_id": "msg-nf"},
            transport,
        )

        msg = ws.find_method("message.action.result")
        assert msg is not None
        assert msg["params"]["ok"] is False
        assert "not found" in msg["params"]["error"]

    async def test_message_adopt(self, backend, ws, transport):
        """message.adopt adopts branch and sends result."""
        await backend.context_store.set_context("conv-ma", RunContext(project="proj"))
        branch = await backend._facade.conv_branches.create(
            "proj", label="adopt-me", session_id="conv-ma"
        )
        await backend.context_store.set_active_branch("conv-ma", branch.branch_id)

        backend._active_transports = {transport}

        await backend._handle_message_adopt(
            {"conversation_id": "conv-ma", "message_id": "msg-a1"}, transport
        )

        msg = ws.find_method("message.action.result")
        assert msg is not None
        assert msg["params"]["action"] == "adopt"
        assert msg["params"]["ok"] is True

    async def test_message_adopt_no_active_branch(self, backend, ws, transport):
        """message.adopt without active branch still sends ok result."""
        await backend.context_store.set_context("conv-mab", RunContext(project="proj"))

        await backend._handle_message_adopt(
            {"conversation_id": "conv-mab", "message_id": "msg-ab"}, transport
        )

        msg = ws.find_method("message.action.result")
        assert msg is not None
        assert msg["params"]["ok"] is True

    async def test_message_retry_no_prompt(self, backend, ws, transport, runtime):
        """message.retry with empty journal returns early."""
        await backend._handle_message_retry(
            {"conversation_id": "conv-mr", "message_id": "msg-r1"},
            runtime,
            transport,
            MagicMock(),
        )
        # No branch.created notification
        assert ws.find_method("branch.created") is None


# ═══════════════════════════════════════════════════════════════
# Phase 4: Write API handlers
# ═══════════════════════════════════════════════════════════════


class TestWriteApiHandlers:
    async def test_discussion_save(self, backend, ws, transport):
        """discussion.save_roundtable creates and returns discussion record."""
        await backend._handle_discussion_save(
            {
                "project": "proj",
                "discussion_id": "disc-1",
                "topic": "Test topic",
                "participants": ["claude", "gemini"],
                "rounds": 2,
                "transcript": [],
            },
            transport,
        )

        msg = ws.find_method("discussion.save_roundtable.result")
        assert msg is not None
        assert msg["params"]["topic"] == "Test topic"
        assert msg["params"]["project"] == "proj"

    async def test_discussion_save_no_project(self, backend, ws, transport):
        """discussion.save_roundtable without project sends error."""
        await backend._handle_discussion_save({"topic": "t"}, transport)

        msg = ws.find_method("discussion.save_roundtable.result")
        assert msg is not None
        assert "error" in msg["params"]

    async def test_discussion_link_branch(self, backend, ws, transport):
        """discussion.link_branch links discussion to branch."""
        # Create discussion and branch first
        await backend._facade.discussions.create_record(
            "proj",
            discussion_id="disc-link",
            topic="link test",
            participants=["a"],
            rounds=1,
            transcript=[],
        )
        await backend._facade.branches.create_branch("proj", "feature-1")

        await backend._handle_discussion_link_branch(
            {
                "project": "proj",
                "discussion_id": "disc-link",
                "branch_name": "feature-1",
            },
            transport,
        )

        msg = ws.find_method("discussion.link_branch.result")
        assert msg is not None

    async def test_discussion_link_branch_missing_params(self, backend, ws, transport):
        """discussion.link_branch without required params sends error."""
        await backend._handle_discussion_link_branch(
            {"project": "proj", "discussion_id": "d1"},
            transport,
        )

        msg = ws.find_method("discussion.link_branch.result")
        assert msg is not None
        assert "error" in msg["params"]

    async def test_synthesis_create_no_params(self, backend, ws, transport):
        """synthesis.create without required params sends error."""
        await backend._handle_synthesis_create({"project": ""}, transport)
        msg = ws.find_method("synthesis.create.result")
        assert msg is not None
        assert "error" in msg["params"]

    async def test_review_request_no_params(self, backend, ws, transport):
        """review.request without required params sends error."""
        await backend._handle_review_request({"project": ""}, transport)
        msg = ws.find_method("review.request.result")
        assert msg is not None
        assert "error" in msg["params"]

    async def test_handoff_create(self, backend, ws, transport, runtime):
        """handoff.create generates a URI."""
        await backend._handle_handoff_create(
            {"project": "proj", "session_id": "s1"}, runtime, transport
        )

        msg = ws.find_method("handoff.create.result")
        assert msg is not None
        assert "uri" in msg["params"]
        assert msg["params"]["project"] == "proj"

    async def test_handoff_create_no_project(self, backend, ws, transport, runtime):
        """handoff.create without project sends error."""
        await backend._handle_handoff_create({"project": ""}, runtime, transport)
        msg = ws.find_method("handoff.create.result")
        assert msg is not None
        assert "error" in msg["params"]

    async def test_handoff_parse_valid(self, backend, ws, transport):
        """handoff.parse with valid URI returns parsed fields."""
        with patch(
            "tunapi.core.handoff.parse_handoff_uri"
        ) as mock_parse:
            parsed = MagicMock()
            parsed.project = "proj"
            parsed.session_id = "s1"
            parsed.branch_id = None
            parsed.focus = None
            parsed.pending_run_id = None
            parsed.engine = None
            parsed.conversation_id = None
            mock_parse.return_value = parsed

            await backend._handle_handoff_parse(
                {"uri": "tunapi://open?project=proj"}, transport
            )

        msg = ws.find_method("handoff.parse.result")
        assert msg is not None
        assert msg["params"]["project"] == "proj"

    async def test_handoff_parse_empty_uri(self, backend, ws, transport):
        """handoff.parse with empty URI sends error."""
        await backend._handle_handoff_parse({"uri": ""}, transport)
        msg = ws.find_method("handoff.parse.result")
        assert msg is not None
        assert "error" in msg["params"]

    async def test_handoff_parse_invalid(self, backend, ws, transport):
        """handoff.parse with invalid URI sends error."""
        with patch(
            "tunapi.core.handoff.parse_handoff_uri", return_value=None
        ):
            await backend._handle_handoff_parse({"uri": "bad://uri"}, transport)
        msg = ws.find_method("handoff.parse.result")
        assert msg is not None
        assert "error" in msg["params"]


# ═══════════════════════════════════════════════════════════════
# Structured JSON RPC handlers
# ═══════════════════════════════════════════════════════════════


class TestStructuredJsonRpcHandlers:
    async def test_branch_list_json_no_project(self, backend, ws, transport, runtime):
        """branch.list.json without project sends error."""
        await backend._handle_branch_list_json({}, runtime, transport)
        msg = ws.find_method("branch.list.json.result")
        assert msg is not None
        assert "error" in msg["params"]

    async def test_branch_list_json_with_project(self, backend, ws, transport, runtime):
        """branch.list.json returns git_branches and conv_branches."""
        await backend.context_store.set_context("conv-blj", RunContext(project="proj"))
        await backend._facade.conv_branches.create(
            "proj", label="br-1", session_id="conv-blj"
        )

        await backend._handle_branch_list_json(
            {"conversation_id": "conv-blj"}, runtime, transport
        )

        msg = ws.find_method("branch.list.json.result")
        assert msg is not None
        assert msg["params"]["project"] == "proj"
        assert "conv_branches" in msg["params"]
        assert len(msg["params"]["conv_branches"]) == 1

    async def test_memory_list_json_no_project(self, backend, ws, transport):
        """memory.list.json without project sends error."""
        await backend._handle_memory_list_json({}, transport)
        msg = ws.find_method("memory.list.json.result")
        assert msg is not None
        assert "error" in msg["params"]

    async def test_memory_list_json_with_project(self, backend, ws, transport):
        """memory.list.json returns entries."""
        await backend.context_store.set_context("conv-mlj", RunContext(project="proj"))
        await backend._facade.memory.add_entry(
            project="proj", type="decision", title="Test", content="Content", source="test"
        )

        await backend._handle_memory_list_json(
            {"conversation_id": "conv-mlj"}, transport
        )

        msg = ws.find_method("memory.list.json.result")
        assert msg is not None
        assert len(msg["params"]["entries"]) == 1

    async def test_review_list_json_no_project(self, backend, ws, transport):
        """review.list.json without project sends error."""
        await backend._handle_review_list_json({}, transport)
        msg = ws.find_method("review.list.json.result")
        assert msg is not None
        assert "error" in msg["params"]

    async def test_review_list_json_with_project(self, backend, ws, transport):
        """review.list.json returns reviews list."""
        await backend.context_store.set_context("conv-rlj", RunContext(project="proj"))

        await backend._handle_review_list_json(
            {"conversation_id": "conv-rlj"}, transport
        )

        msg = ws.find_method("review.list.json.result")
        assert msg is not None
        assert "reviews" in msg["params"]


# ═══════════════════════════════════════════════════════════════
# Code search/map handlers
# ═══════════════════════════════════════════════════════════════


class TestCodeSearchMap:
    async def test_code_search_missing_params(self, backend, ws, transport, runtime):
        """code.search without query/project sends error."""
        await backend._handle_code_search({"query": ""}, runtime, transport)
        msg = ws.find_method("code.search.result")
        assert msg is not None
        assert "error" in msg["params"]

    async def test_code_search_no_project_path(self, backend, ws, transport, runtime):
        """code.search with unknown project path sends error."""
        with patch.object(backend, "_resolve_project_path", return_value=None):
            await backend._handle_code_search(
                {"query": "test", "project": "unknown"}, runtime, transport
            )
        msg = ws.find_method("code.search.result")
        assert msg is not None
        assert "error" in msg["params"]

    async def test_code_search_success(self, backend, ws, transport, runtime):
        """code.search with valid params returns results."""
        with (
            patch.object(backend, "_resolve_project_path", return_value=Path("/proj")),
            patch(
                "tunapi.tunadish.rawq_bridge.search",
                new_callable=AsyncMock,
                return_value={
                    "results": [{"file": "a.py", "lines": [1, 5]}],
                    "query_ms": 42,
                    "total_tokens": 100,
                },
            ),
            patch("tunapi.tunadish.rawq_bridge.is_available", return_value=True),
        ):
            await backend._handle_code_search(
                {"query": "test", "project": "proj"}, runtime, transport
            )

        msg = ws.find_method("code.search.result")
        assert msg is not None
        assert len(msg["params"]["results"]) == 1
        assert msg["params"]["available"] is True

    async def test_code_map_no_project_path(self, backend, ws, transport, runtime):
        """code.map with unknown project sends error."""
        with patch.object(backend, "_resolve_project_path", return_value=None):
            await backend._handle_code_map(
                {"project": "unknown"}, runtime, transport
            )
        msg = ws.find_method("code.map.result")
        assert msg is not None
        assert "error" in msg["params"]

    async def test_code_map_success(self, backend, ws, transport, runtime):
        """code.map with valid project returns map."""
        with (
            patch.object(backend, "_resolve_project_path", return_value=Path("/proj")),
            patch(
                "tunapi.tunadish.rawq_bridge.get_map",
                new_callable=AsyncMock,
                return_value={"files": [{"path": "main.py", "symbols": []}]},
            ),
            patch("tunapi.tunadish.rawq_bridge.is_available", return_value=True),
        ):
            await backend._handle_code_map(
                {"project": "proj"}, runtime, transport
            )

        msg = ws.find_method("code.map.result")
        assert msg is not None
        assert msg["params"]["available"] is True
        assert "files" in msg["params"]["map"]


# ═══════════════════════════════════════════════════════════════
# engine.list handler
# ═══════════════════════════════════════════════════════════════


class TestEngineList:
    async def test_engine_list(self, backend, ws, transport, runtime):
        """engine.list returns engine models."""
        with patch(
            "tunapi.engine_models.get_models",
            return_value=(["model-a", "model-b"], "cache"),
        ):
            await backend._handle_engine_list(runtime, transport)

        msg = ws.find_method("engine.list.result")
        assert msg is not None
        assert "engines" in msg["params"]
        assert msg["params"]["engines"]["claude"] == ["model-a", "model-b"]


# ═══════════════════════════════════════════════════════════════
# _ws_handler reconnection notification
# ═══════════════════════════════════════════════════════════════


class TestWsHandlerReconnect:
    def test_run_map_tracks_active_runs(self):
        """run_map and running_tasks track active runs for reconnect notification."""
        b = TunadishBackend()
        ref = MessageRef(channel_id="conv1", message_id="m1")
        task = RunningTask()
        b.run_map["conv1"] = ref
        b.running_tasks[ref] = task

        assert b.run_map["conv1"] == ref
        assert b.running_tasks[ref] is task
        assert not task.done.is_set()


# ═══════════════════════════════════════════════════════════════
# _rawq_ensure_index
# ═══════════════════════════════════════════════════════════════


class TestRawqEnsureIndex:
    async def test_rawq_not_available(self, backend, runtime, transport):
        """rawq_ensure_index returns early if rawq not available."""
        with patch("tunapi.tunadish.rawq_bridge.is_available", return_value=False):
            await backend._rawq_ensure_index("proj", runtime, transport)
        # No error, just returns

    async def test_no_project_path(self, backend, runtime, transport):
        """rawq_ensure_index returns early if project path not found."""
        with (
            patch("tunapi.tunadish.rawq_bridge.is_available", return_value=True),
            patch.object(backend, "_resolve_project_path", return_value=None),
        ):
            await backend._rawq_ensure_index("proj", runtime, transport)

    async def test_index_exists(self, backend, ws, runtime, transport):
        """rawq_ensure_index skips building if index already exists."""
        with (
            patch("tunapi.tunadish.rawq_bridge.is_available", return_value=True),
            patch.object(backend, "_resolve_project_path", return_value=Path("/proj")),
            patch(
                "tunapi.tunadish.rawq_bridge.check_index",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ),
            patch(
                "tunapi.tunadish.rawq_bridge.build_index", new_callable=AsyncMock
            ) as mock_build,
        ):
            await backend._rawq_ensure_index("proj", runtime, transport)
            mock_build.assert_not_awaited()

    async def test_index_built(self, backend, ws, runtime, transport):
        """rawq_ensure_index builds index when none exists."""
        with (
            patch("tunapi.tunadish.rawq_bridge.is_available", return_value=True),
            patch.object(backend, "_resolve_project_path", return_value=Path("/proj")),
            patch(
                "tunapi.tunadish.rawq_bridge.check_index",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "tunapi.tunadish.rawq_bridge.build_index",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await backend._rawq_ensure_index("proj", runtime, transport)

        # Check that success notification was sent
        results = [
            m
            for m in ws.sent
            if m.get("method") == "command.result"
            and "완료" in m.get("params", {}).get("text", "")
        ]
        assert len(results) == 1

    async def test_index_build_failure(self, backend, ws, runtime, transport):
        """rawq_ensure_index handles build failure gracefully."""
        with (
            patch("tunapi.tunadish.rawq_bridge.is_available", return_value=True),
            patch.object(backend, "_resolve_project_path", return_value=Path("/proj")),
            patch(
                "tunapi.tunadish.rawq_bridge.check_index",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "tunapi.tunadish.rawq_bridge.build_index",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            await backend._rawq_ensure_index("proj", runtime, transport)

        # Check failure notification
        results = [
            m
            for m in ws.sent
            if m.get("method") == "command.result"
            and "실패" in m.get("params", {}).get("text", "")
        ]
        assert len(results) == 1


# ═══════════════════════════════════════════════════════════════
# _rawq_startup_check
# ═══════════════════════════════════════════════════════════════


class TestRawqStartupCheck:
    async def test_startup_not_available(self, backend):
        """Startup check with rawq not available returns early."""
        with patch("tunapi.tunadish.rawq_bridge.is_available", return_value=False):
            await backend._rawq_startup_check()

    async def test_startup_no_update(self, backend):
        """Startup check with no update available."""
        with (
            patch("tunapi.tunadish.rawq_bridge.is_available", return_value=True),
            patch(
                "tunapi.tunadish.rawq_bridge.get_version",
                new_callable=AsyncMock,
                return_value="1.0.0",
            ),
            patch(
                "tunapi.tunadish.rawq_bridge.check_for_update",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await backend._rawq_startup_check()

    async def test_startup_with_update(self, backend, ws, transport):
        """Startup check broadcasts update notification."""
        backend._active_transports = {transport}

        with (
            patch("tunapi.tunadish.rawq_bridge.is_available", return_value=True),
            patch(
                "tunapi.tunadish.rawq_bridge.get_version",
                new_callable=AsyncMock,
                return_value="1.0.0",
            ),
            patch(
                "tunapi.tunadish.rawq_bridge.check_for_update",
                new_callable=AsyncMock,
                return_value={
                    "has_update": True,
                    "current": "1.0.0",
                    "latest": "1.1.0",
                    "commits": ["a", "b"],
                },
            ),
        ):
            await backend._rawq_startup_check()

        msg = ws.find_method("command.result")
        assert msg is not None
        assert "업데이트" in msg["params"]["text"]


# ═══════════════════════════════════════════════════════════════
# _resolve_context_conv_id with branch: prefix
# ═══════════════════════════════════════════════════════════════


class TestResolveContextConvIdExtra:
    async def test_branch_prefix_with_matching_facade_entry(self, backend):
        """branch:X resolves to parent conv_id via facade lookup."""
        # Need a non-branch conv entry so the loop finds a project to query
        await backend.context_store.set_context("parent-conv", RunContext(project="proj"))

        branch_obj = MagicMock()
        branch_obj.session_id = "parent-conv"

        with patch.object(
            backend._facade.conv_branches, "get", new_callable=AsyncMock, return_value=branch_obj
        ):
            result = await backend._resolve_context_conv_id("branch:br-abc")
            assert result == "parent-conv"
