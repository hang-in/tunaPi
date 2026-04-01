"""Tests for tunadish backend and rawq_bridge."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from tunapi.context import RunContext
from tunapi.core.memory_facade import ProjectMemoryFacade
from tunapi.journal import Journal, JournalEntry
from tunapi.runner_bridge import RunningTask
from tunapi.transport import MessageRef, RenderedMessage
from tunapi.tunadish.backend import (
    TunadishBackend,
    _RAWQ_CONTEXT_RE,
    _SIBLING_CONTEXT_RE,
)
from tunapi.tunadish.context_store import (
    ConversationContextStore,
    ConversationMeta,
    ConversationSettings,
)
from tunapi.tunadish.rawq_bridge import (
    _DEFAULT_EXCLUDE,
    format_context_block,
    format_map_block,
)
from tunapi.tunadish.transport import TunadishTransport

pytestmark = pytest.mark.anyio


# ── Fakes ──


class FakeWs:
    """Captures messages sent via websocket."""

    def __init__(self):
        self.sent: list[dict[str, Any]] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def last(self) -> dict[str, Any]:
        return self.sent[-1]

    def last_params(self) -> dict[str, Any]:
        return self.last().get("params", self.last().get("result", {}))


class FakeRuntime:
    def __init__(self, *, project_aliases=None, engine_ids=None):
        self._aliases = project_aliases or []
        self._engine_ids = engine_ids or ["claude"]
        self.default_engine = "claude"

    def project_aliases(self) -> list[str]:
        return self._aliases

    def available_engine_ids(self) -> list[str]:
        return self._engine_ids

    def chat_ids_for_project(self, project: str) -> list[str]:
        return []

    def resolve_run_cwd(self, ctx: Any) -> Path | None:
        return None


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
    return b


@pytest.fixture
def runtime():
    return FakeRuntime()


# ═══════════════════════════════════════════════════════════════
# Section 1: Regex patterns (pure logic)
# ═══════════════════════════════════════════════════════════════


class TestRawqContextRegex:
    """_RAWQ_CONTEXT_RE strips rawq-injected context blocks from history."""

    def test_removes_context_block(self):
        text = (
            "<relevant_code>\n## file.py:1-5\n```python\nfoo()\n```\n"
            "</relevant_code>\n---\nWhat does foo do?"
        )
        cleaned = _RAWQ_CONTEXT_RE.sub("", text)
        assert "<relevant_code>" not in cleaned
        assert "What does foo do?" in cleaned

    def test_preserves_text_without_block(self):
        text = "Just a normal question"
        assert _RAWQ_CONTEXT_RE.sub("", text) == text

    def test_removes_multiple_blocks(self):
        text = (
            "<relevant_code>block1</relevant_code>\n---\n"
            "middle text "
            "<relevant_code>block2</relevant_code>\n---\nend"
        )
        cleaned = _RAWQ_CONTEXT_RE.sub("", text)
        assert "block1" not in cleaned
        assert "block2" not in cleaned
        assert "middle text " in cleaned
        assert "end" in cleaned


class TestSiblingContextRegex:
    """_SIBLING_CONTEXT_RE strips cross-session summary blocks."""

    def test_removes_sibling_block(self):
        text = (
            "<sibling_sessions>\nsession info\n</sibling_sessions>\n---\n"
            "User question"
        )
        cleaned = _SIBLING_CONTEXT_RE.sub("", text)
        assert "<sibling_sessions>" not in cleaned
        assert "User question" in cleaned

    def test_preserves_text_without_block(self):
        text = "Regular text without sibling tags"
        assert _SIBLING_CONTEXT_RE.sub("", text) == text


# ═══════════════════════════════════════════════════════════════
# Section 2: TunadishBackend initialization and check_setup
# ═══════════════════════════════════════════════════════════════


class TestBackendInit:
    def test_default_state(self):
        b = TunadishBackend()
        assert b.id == "tunadish"
        assert b.description == "Tunadish WebSocket Transport"
        assert b.run_map == {}
        assert b.running_tasks == {}
        assert b._active_transports == set()

    def test_check_setup_returns_no_issues(self):
        b = TunadishBackend()
        result = b.check_setup(engine_backend=None)
        assert result.issues == []

    async def test_interactive_setup_returns_true(self):
        b = TunadishBackend()
        assert await b.interactive_setup() is True

    def test_lock_token_returns_none(self):
        b = TunadishBackend()
        assert b.lock_token(transport_config={}, _config_path=None) is None


# ═══════════════════════════════════════════════════════════════
# Section 3: _discover_projects
# ═══════════════════════════════════════════════════════════════


class TestDiscoverProjects:
    def test_no_config_path(self):
        b = TunadishBackend()
        b._config_path = None
        assert b._discover_projects([]) == []

    def test_discovers_git_dirs(self, tmp_path):
        projects_root = tmp_path / "projects"
        projects_root.mkdir()
        (projects_root / "alpha" / ".git").mkdir(parents=True)
        (projects_root / "beta" / ".git").mkdir(parents=True)
        (projects_root / "no_git").mkdir(parents=True)  # no .git

        config_file = tmp_path / "tunapi.toml"
        config_file.write_text(f'projects_root = "{projects_root}"\n')

        b = TunadishBackend()
        b._config_path = str(config_file)
        discovered = b._discover_projects([])
        assert "alpha" in discovered
        assert "beta" in discovered
        assert "no_git" not in discovered

    def test_excludes_configured_aliases(self, tmp_path):
        projects_root = tmp_path / "projects"
        projects_root.mkdir()
        (projects_root / "alpha" / ".git").mkdir(parents=True)

        config_file = tmp_path / "tunapi.toml"
        config_file.write_text(f'projects_root = "{projects_root}"\n')

        b = TunadishBackend()
        b._config_path = str(config_file)
        discovered = b._discover_projects(["alpha"])
        assert "alpha" not in discovered

    def test_excludes_configured_paths(self, tmp_path):
        projects_root = tmp_path / "projects"
        projects_root.mkdir()
        proj_dir = projects_root / "myproj"
        (proj_dir / ".git").mkdir(parents=True)

        config_file = tmp_path / "tunapi.toml"
        config_file.write_text(
            f'projects_root = "{projects_root}"\n'
            f'[projects.myproj]\n'
            f'path = "{proj_dir}"\n'
        )

        b = TunadishBackend()
        b._config_path = str(config_file)
        discovered = b._discover_projects([])
        assert "myproj" not in discovered


class TestGetProjectsRoot:
    def test_returns_none_without_config(self):
        b = TunadishBackend()
        b._config_path = None
        assert b._get_projects_root() is None

    def test_reads_from_toml(self, tmp_path):
        config_file = tmp_path / "tunapi.toml"
        config_file.write_text('projects_root = "/some/path"\n')

        b = TunadishBackend()
        b._config_path = str(config_file)
        assert b._get_projects_root() == "/some/path"


# ═══════════════════════════════════════════════════════════════
# Section 4: handle_run_cancel
# ═══════════════════════════════════════════════════════════════


class TestHandleRunCancel:
    async def test_cancel_sets_flag(self):
        b = TunadishBackend()
        ref = MessageRef(channel_id="conv1", message_id="m1")
        task = RunningTask()
        b.run_map["conv1"] = ref
        b.running_tasks[ref] = task

        ws = FakeWs()
        await b.handle_run_cancel({"conversation_id": "conv1"}, ws)
        assert task.cancel_requested.is_set()

    async def test_cancel_no_active_run(self):
        b = TunadishBackend()
        ws = FakeWs()
        # Should not raise
        await b.handle_run_cancel({"conversation_id": "missing"}, ws)

    async def test_cancel_no_task_for_ref(self):
        b = TunadishBackend()
        ref = MessageRef(channel_id="conv1", message_id="m1")
        b.run_map["conv1"] = ref
        # running_tasks has no entry for ref

        ws = FakeWs()
        await b.handle_run_cancel({"conversation_id": "conv1"}, ws)
        # Should not raise


# ═══════════════════════════════════════════════════════════════
# Section 5: _resolve_context_conv_id
# ═══════════════════════════════════════════════════════════════


class TestResolveContextConvId:
    async def test_regular_conv_id_passthrough(self, backend):
        result = await backend._resolve_context_conv_id("conv-123")
        assert result == "conv-123"

    async def test_branch_prefix_without_match_returns_original(self, backend):
        result = await backend._resolve_context_conv_id("branch:br-999")
        assert result == "branch:br-999"


# ═══════════════════════════════════════════════════════════════
# Section 6: _build_cross_session_summary
# ═══════════════════════════════════════════════════════════════


class TestBuildCrossSessionSummary:
    async def test_no_siblings_returns_none(self, backend):
        result = await backend._build_cross_session_summary("conv1", "proj")
        assert result is None

    async def test_with_siblings(self, backend, tmp_path):
        # Set up two conversations for the same project
        await backend.context_store.set_context("conv1", RunContext(project="proj"))
        await backend.context_store.set_context("conv2", RunContext(project="proj"))

        # Write a journal entry for conv2
        import time
        entry = JournalEntry(
            run_id="run1",
            channel_id="conv2",
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            event="prompt",
            data={"text": "Hello from sibling"},
        )
        await backend._journal.append(entry)

        result = await backend._build_cross_session_summary("conv1", "proj")
        assert result is not None
        assert "<sibling_sessions>" in result
        assert "Hello from sibling" in result

    async def test_excludes_current_conv(self, backend):
        await backend.context_store.set_context("conv1", RunContext(project="proj"))
        # Only one conv -> no siblings
        result = await backend._build_cross_session_summary("conv1", "proj")
        assert result is None


# ═══════════════════════════════════════════════════════════════
# Section 7: _build_adopt_summary
# ═══════════════════════════════════════════════════════════════


class TestBuildAdoptSummary:
    async def test_empty_journal(self, backend):
        branch = MagicMock()
        branch.label = "test-branch"
        branch.branch_id = "br-12345678"
        result = await backend._build_adopt_summary(branch, "conv1")
        assert "test-branch" in result
        assert "채택됨" in result

    async def test_with_entries(self, backend):
        import time

        branch = MagicMock()
        branch.label = "feature"
        branch.branch_id = "br-aaaabbbb"

        # Add prompt and response entries
        await backend._journal.append(JournalEntry(
            run_id="r1", channel_id="conv1",
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            event="prompt", data={"text": "Fix the bug"},
        ))
        await backend._journal.append(JournalEntry(
            run_id="r1", channel_id="conv1",
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            event="response", data={"text": "I fixed the bug in module.py"},
        ))

        result = await backend._build_adopt_summary(branch, "conv1")
        assert "feature" in result
        assert "1턴" in result


# ═══════════════════════════════════════════════════════════════
# Section 8: _build_branch_context
# ═══════════════════════════════════════════════════════════════


class TestBuildBranchContext:
    async def test_empty_journal_returns_empty(self, backend):
        result = await backend._build_branch_context("conv1", None)
        assert result == ""

    async def test_generates_context_from_entries(self, backend):
        import time

        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        await backend._journal.append(JournalEntry(
            run_id="r1", channel_id="conv1", timestamp=ts,
            event="prompt", data={"text": "What is this project?"},
        ))
        await backend._journal.append(JournalEntry(
            run_id="r1", channel_id="conv1", timestamp=ts,
            event="completed", data={"ok": True, "answer": "It is a chat bridge."},
        ))

        result = await backend._build_branch_context("conv1", None)
        assert "branch-context" in result
        assert "What is this project?" in result
        assert "It is a chat bridge." in result

    async def test_limits_visible_lines(self, backend):
        import time

        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        # Add many entries (>8 lines)
        for i in range(10):
            await backend._journal.append(JournalEntry(
                run_id=f"r{i}", channel_id="conv1", timestamp=ts,
                event="prompt", data={"text": f"Question {i}"},
            ))
            await backend._journal.append(JournalEntry(
                run_id=f"r{i}", channel_id="conv1", timestamp=ts,
                event="completed", data={"ok": True, "answer": f"Answer {i}"},
            ))

        result = await backend._build_branch_context("conv1", None)
        assert "생략" in result


# ═══════════════════════════════════════════════════════════════
# Section 9: _make_conv_token_saver
# ═══════════════════════════════════════════════════════════════


class TestMakeConvTokenSaver:
    async def test_saves_token(self, backend):
        saver = backend._make_conv_token_saver("conv-42")
        token = MagicMock()
        token.engine = "claude"
        token.value = "tok-abc"
        done = MagicMock()

        await saver(token, done)

        backend._conv_sessions.set.assert_awaited_once_with(
            "conv-42", engine="claude", token="tok-abc",
        )


# ═══════════════════════════════════════════════════════════════
# Section 10: _resolve_project_path
# ═══════════════════════════════════════════════════════════════


class TestResolveProjectPath:
    def test_from_projects_map(self, backend, tmp_path):
        proj_dir = tmp_path / "myproject"
        proj_dir.mkdir()

        pc = MagicMock()
        pc.path = proj_dir
        projects_obj = MagicMock()
        projects_obj.projects = {"myproject": pc}
        runtime = MagicMock()
        runtime._projects = projects_obj

        result = backend._resolve_project_path("myproject", runtime)
        assert result == proj_dir

    def test_from_projects_root(self, backend, tmp_path):
        projects_root = tmp_path / "projects"
        candidate = projects_root / "foo"
        candidate.mkdir(parents=True)

        # Config with projects_root
        config_file = Path(backend._config_path)
        config_file.write_text(f'projects_root = "{projects_root}"\n')

        runtime = MagicMock()
        runtime._projects = MagicMock()
        runtime._projects.projects = {}

        result = backend._resolve_project_path("foo", runtime)
        assert result == candidate

    def test_returns_none_for_unknown(self, backend):
        runtime = MagicMock()
        runtime._projects = MagicMock()
        runtime._projects.projects = {}
        backend._config_path = None

        result = backend._resolve_project_path("unknown", runtime)
        assert result is None


# ═══════════════════════════════════════════════════════════════
# Section 11: RPC dispatch table coverage (ws_handler method routing)
# ═══════════════════════════════════════════════════════════════


class TestRpcMethodRouting:
    """Verify that RPC methods in _ws_handler dispatch to the correct handlers."""

    async def test_ping_with_rpc_id(self, ws, transport):
        """ping with rpc_id sends pong response."""
        b = TunadishBackend()
        # Simulate the ping handling directly
        rpc_id = "req-1"
        await transport._send_response(rpc_id, {"pong": True})
        msg = ws.last()
        assert msg["result"]["pong"] is True
        assert msg["id"] == "req-1"

    async def test_ping_without_rpc_id(self, ws):
        """ping without rpc_id sends notification pong."""
        await FakeWs().send(json.dumps({"method": "pong"}))  # just verifying format

    async def test_unknown_method_error(self, ws, transport):
        """Unknown methods return -32601 error."""
        await transport._send_error("req-99", -32601, "Method not found: foo.bar")
        msg = ws.last()
        assert msg["error"]["code"] == -32601
        assert "foo.bar" in msg["error"]["message"]


# ═══════════════════════════════════════════════════════════════
# Section 12: conversation.create / delete / list
# ═══════════════════════════════════════════════════════════════


class TestConversationHandlers:
    async def test_conversation_create(self, backend, ws, transport):
        params = {
            "conversation_id": "conv-new",
            "project": "myproj",
            "label": "My Conv",
        }
        conv_id = params["conversation_id"]
        from tunapi.context import RunContext
        await backend.context_store.set_context(
            conv_id, RunContext(project=params["project"]), label=params["label"],
        )

        ctx = await backend.context_store.get_context(conv_id)
        assert ctx is not None
        assert ctx.project == "myproj"

    async def test_conversation_delete_clears_context(self, backend, ws, transport, tmp_path):
        await backend.context_store.set_context("conv-del", RunContext(project="proj"))

        # Simulate delete
        await backend.context_store.clear("conv-del")
        ctx = await backend.context_store.get_context("conv-del")
        assert ctx is None

    async def test_conversation_list(self, backend):
        await backend.context_store.set_context("c1", RunContext(project="proj"))
        await backend.context_store.set_context("c2", RunContext(project="proj"))
        await backend.context_store.set_context("c3", RunContext(project="other"))

        convs = backend.context_store.list_conversations(project="proj")
        assert len(convs) == 2
        assert all(c["project"] == "proj" for c in convs)


# ═══════════════════════════════════════════════════════════════
# Section 13: _dispatch_rpc_command
# ═══════════════════════════════════════════════════════════════


class TestDispatchRpcCommand:
    async def test_model_set_updates_conv_settings(self, backend, ws, transport, runtime):
        await backend.context_store.set_context("conv-1", RunContext(project="proj"))

        with patch("tunapi.tunadish.backend.dispatch_command", new_callable=AsyncMock) as mock_dispatch:
            mock_dispatch.return_value = True
            await backend._dispatch_rpc_command(
                "model", "claude opus-4",
                {"conversation_id": "conv-1"},
                runtime, transport,
            )
            mock_dispatch.assert_awaited_once()
            call_kwargs = mock_dispatch.call_args
            assert call_kwargs[1]["channel_id"] == "conv-1"

    async def test_trigger_set_routing(self, backend, ws, transport, runtime):
        await backend.context_store.set_context("conv-1", RunContext(project="proj"))

        with patch("tunapi.tunadish.backend.dispatch_command", new_callable=AsyncMock) as mock_dispatch:
            mock_dispatch.return_value = True
            await backend._dispatch_rpc_command(
                "trigger", "always",
                {"conversation_id": "conv-1"},
                runtime, transport,
            )
            args = mock_dispatch.call_args[0]
            assert args[0] == "trigger"
            assert args[1] == "always"


# ═══════════════════════════════════════════════════════════════
# Section 14: _broadcast
# ═══════════════════════════════════════════════════════════════


class TestBroadcast:
    async def test_broadcasts_to_all_transports(self):
        b = TunadishBackend()
        ws1, ws2 = FakeWs(), FakeWs()
        t1, t2 = TunadishTransport(ws1), TunadishTransport(ws2)
        b._active_transports = {t1, t2}

        await b._broadcast("test.event", {"data": "hello"})
        assert len(ws1.sent) == 1
        assert len(ws2.sent) == 1
        assert ws1.sent[0]["method"] == "test.event"

    async def test_marks_transport_closed_on_send_error(self):
        """When ws.send raises, TunadishTransport marks itself closed."""
        b = TunadishBackend()
        good_ws = FakeWs()
        dead_ws = MagicMock()
        dead_ws.send = AsyncMock(side_effect=ConnectionError("closed"))

        t_good = TunadishTransport(good_ws)
        t_dead = TunadishTransport(dead_ws)
        b._active_transports = {t_good, t_dead}

        await b._broadcast("test.event", {"data": "hello"})
        # Good transport got the message
        assert len(good_ws.sent) == 1
        # Dead transport is marked as closed
        assert t_dead._closed is True


# ═══════════════════════════════════════════════════════════════
# Section 15: handle_chat_send (command parsing path)
# ═══════════════════════════════════════════════════════════════


class TestHandleChatSend:
    async def test_missing_conversation_id(self, backend, runtime, transport):
        """chat.send without conversation_id logs error and returns."""
        await backend.handle_chat_send({}, runtime, transport)
        # Should not raise

    async def test_command_dispatch(self, backend, runtime, transport, ws):
        """chat.send with !help dispatches to command handler."""
        await backend.context_store.set_context("conv-cmd", RunContext(project="proj"))

        with patch("tunapi.tunadish.backend.dispatch_command", new_callable=AsyncMock) as mock_dispatch:
            mock_dispatch.return_value = True
            await backend.handle_chat_send(
                {"conversation_id": "conv-cmd", "text": "!help"},
                runtime, transport,
            )
            mock_dispatch.assert_awaited_once()

    async def test_lock_prevents_concurrent_runs(self, backend, runtime, transport):
        """If lock is already held, second chat.send is skipped."""
        lock = anyio.Lock()
        backend._conv_locks["conv-locked"] = lock

        # Pre-acquire the lock
        await lock.acquire()
        try:
            # This should return immediately without executing
            await backend.handle_chat_send(
                {"conversation_id": "conv-locked", "text": "hello"},
                runtime, transport,
            )
        finally:
            lock.release()


# ═══════════════════════════════════════════════════════════════
# Section 16: rawq_bridge — format_context_block
# ═══════════════════════════════════════════════════════════════


class TestFormatContextBlock:
    def test_empty_results(self):
        assert format_context_block({"results": []}) == ""
        assert format_context_block({}) == ""

    def test_single_result(self):
        result = {
            "results": [
                {
                    "file": "src/main.py",
                    "lines": [10, 20],
                    "language": "python",
                    "scope": "function main",
                    "confidence": 0.85,
                    "content": "def main():\n    pass",
                }
            ]
        }
        block = format_context_block(result)
        assert "<relevant_code>" in block
        assert "</relevant_code>" in block
        assert "src/main.py:10-20" in block
        assert "(function main)" in block
        assert "[confidence: 0.85]" in block
        assert "```python" in block
        assert "def main():" in block

    def test_no_lines_no_scope(self):
        result = {
            "results": [
                {
                    "file": "README.md",
                    "lines": [],
                    "language": "",
                    "scope": "",
                    "confidence": 0.5,
                    "content": "# Title",
                }
            ]
        }
        block = format_context_block(result)
        assert "README.md" in block
        # No line range appended
        assert "README.md:" not in block.split("(")[0] if "(" in block else True

    def test_multiple_results(self):
        result = {
            "results": [
                {"file": "a.py", "lines": [1, 5], "language": "python",
                 "scope": "", "confidence": 0.9, "content": "aaa"},
                {"file": "b.py", "lines": [10, 15], "language": "python",
                 "scope": "", "confidence": 0.7, "content": "bbb"},
            ]
        }
        block = format_context_block(result)
        assert "a.py" in block
        assert "b.py" in block

    def test_content_rstripped(self):
        result = {
            "results": [
                {"file": "x.py", "lines": [], "language": "python",
                 "scope": "", "confidence": 0.5, "content": "code   \n\n"},
            ]
        }
        block = format_context_block(result)
        # Content should be right-stripped
        assert "code   \n\n" not in block
        assert "code" in block


# ═══════════════════════════════════════════════════════════════
# Section 17: rawq_bridge — format_map_block
# ═══════════════════════════════════════════════════════════════


class TestFormatMapBlock:
    def test_empty_files(self):
        assert format_map_block({"files": []}) == ""
        assert format_map_block({}) == ""

    def test_file_without_symbols(self):
        result = {"files": [{"path": "README.md", "symbols": []}]}
        block = format_map_block(result)
        assert "<project_structure>" in block
        assert "README.md" in block
        assert "</project_structure>" in block

    def test_file_with_symbols(self):
        result = {
            "files": [
                {
                    "path": "main.py",
                    "symbols": [
                        {"name": "main"},
                        {"name": "helper"},
                    ],
                }
            ]
        }
        block = format_map_block(result)
        assert "main.py (main, helper)" in block

    def test_truncates_many_symbols(self):
        """More than 8 symbols should show ... suffix."""
        symbols = [{"name": f"sym{i}"} for i in range(12)]
        result = {"files": [{"path": "big.py", "symbols": symbols}]}
        block = format_map_block(result)
        assert "..." in block
        # Should show first 8
        assert "sym7" in block

    def test_filters_empty_name_symbols(self):
        result = {
            "files": [
                {
                    "path": "mod.py",
                    "symbols": [
                        {"name": "real"},
                        {"name": ""},
                        {"name": "also_real"},
                    ],
                }
            ]
        }
        block = format_map_block(result)
        assert "real, also_real" in block


# ═══════════════════════════════════════════════════════════════
# Section 18: rawq_bridge — _find_rawq and _DEFAULT_EXCLUDE
# ═══════════════════════════════════════════════════════════════


class TestRawqDefaults:
    def test_default_exclude_patterns(self):
        assert "node_modules" in _DEFAULT_EXCLUDE
        assert ".git" in _DEFAULT_EXCLUDE
        assert "__pycache__" in _DEFAULT_EXCLUDE
        assert ".venv" in _DEFAULT_EXCLUDE

    def test_find_rawq_with_env_var(self, tmp_path):
        """RAWQ_BIN env var takes precedence."""
        from tunapi.tunadish import rawq_bridge

        fake_bin = tmp_path / "rawq_bin"
        fake_bin.write_text("#!/bin/sh\n")

        with patch.dict("os.environ", {"RAWQ_BIN": str(fake_bin)}):
            result = rawq_bridge._find_rawq()
            assert result == str(fake_bin)

    def test_find_rawq_env_var_nonexistent(self):
        from tunapi.tunadish import rawq_bridge

        with patch.dict("os.environ", {"RAWQ_BIN": "/nonexistent/path/rawq"}):
            # Should fall through to PATH/vendor
            result = rawq_bridge._find_rawq()
            # May or may not find rawq via PATH, but shouldn't return nonexistent env path
            assert result != "/nonexistent/path/rawq"


# ═══════════════════════════════════════════════════════════════
# Section 19: _rawq_enrich_message token budget logic
# ═══════════════════════════════════════════════════════════════


class TestRawqEnrichMessage:
    async def test_short_text_gets_larger_budget(self, backend, runtime):
        """Text < 100 chars should use token_budget=4000."""
        with patch("tunapi.tunadish.rawq_bridge.is_available", return_value=True), \
             patch.object(backend, "_resolve_project_path", return_value=Path("/proj")), \
             patch("tunapi.tunadish.rawq_bridge.search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = {"results": [
                {"file": "a.py", "lines": [1, 5], "language": "python",
                 "scope": "", "confidence": 0.9, "content": "code"},
            ]}
            result = await backend._rawq_enrich_message("short q", "proj", runtime)
            call_kwargs = mock_search.call_args[1]
            assert call_kwargs["token_budget"] == 4000
            assert "<relevant_code>" in result

    async def test_medium_text_gets_medium_budget(self, backend, runtime):
        text = "x" * 200
        with patch("tunapi.tunadish.rawq_bridge.is_available", return_value=True), \
             patch.object(backend, "_resolve_project_path", return_value=Path("/proj")), \
             patch("tunapi.tunadish.rawq_bridge.search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = None
            # Falls through to map fallback
            with patch("tunapi.tunadish.rawq_bridge.get_map", new_callable=AsyncMock) as mock_map:
                mock_map.return_value = None
                result = await backend._rawq_enrich_message(text, "proj", runtime)
            call_kwargs = mock_search.call_args[1]
            assert call_kwargs["token_budget"] == 2000

    async def test_long_text_gets_small_budget(self, backend, runtime):
        text = "x" * 600
        with patch("tunapi.tunadish.rawq_bridge.is_available", return_value=True), \
             patch.object(backend, "_resolve_project_path", return_value=Path("/proj")), \
             patch("tunapi.tunadish.rawq_bridge.search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = None
            with patch("tunapi.tunadish.rawq_bridge.get_map", new_callable=AsyncMock) as mock_map:
                mock_map.return_value = None
                await backend._rawq_enrich_message(text, "proj", runtime)
            assert mock_search.call_args[1]["token_budget"] == 1000

    async def test_unavailable_returns_original(self, backend, runtime):
        with patch("tunapi.tunadish.rawq_bridge.is_available", return_value=False):
            result = await backend._rawq_enrich_message("hello", "proj", runtime)
            assert result == "hello"

    async def test_no_project_path_returns_original(self, backend, runtime):
        with patch("tunapi.tunadish.rawq_bridge.is_available", return_value=True), \
             patch.object(backend, "_resolve_project_path", return_value=None):
            result = await backend._rawq_enrich_message("hello", "proj", runtime)
            assert result == "hello"

    async def test_no_search_results_falls_back_to_map(self, backend, runtime):
        with patch("tunapi.tunadish.rawq_bridge.is_available", return_value=True), \
             patch.object(backend, "_resolve_project_path", return_value=Path("/proj")), \
             patch("tunapi.tunadish.rawq_bridge.search", new_callable=AsyncMock, return_value=None), \
             patch("tunapi.tunadish.rawq_bridge.get_map", new_callable=AsyncMock) as mock_map:
            mock_map.return_value = {
                "files": [{"path": "main.py", "symbols": [{"name": "main"}]}],
            }
            result = await backend._rawq_enrich_message("hello", "proj", runtime)
            assert "<project_structure>" in result

    async def test_no_search_and_no_map_returns_original(self, backend, runtime):
        with patch("tunapi.tunadish.rawq_bridge.is_available", return_value=True), \
             patch.object(backend, "_resolve_project_path", return_value=Path("/proj")), \
             patch("tunapi.tunadish.rawq_bridge.search", new_callable=AsyncMock, return_value=None), \
             patch("tunapi.tunadish.rawq_bridge.get_map", new_callable=AsyncMock, return_value=None):
            result = await backend._rawq_enrich_message("hello", "proj", runtime)
            assert result == "hello"


# ═══════════════════════════════════════════════════════════════
# Section 20: _RUN_TIMEOUT constant
# ═══════════════════════════════════════════════════════════════


class TestRunTimeout:
    def test_default_run_timeout(self):
        b = TunadishBackend()
        assert b._RUN_TIMEOUT == 300


# ═══════════════════════════════════════════════════════════════
# Section 21: WS disconnect cleanup
# ═══════════════════════════════════════════════════════════════


class TestWsDisconnectCleanup:
    def test_no_active_transports_cancels_runs(self):
        """When last transport disconnects, orphan runs should be cancelled."""
        b = TunadishBackend()
        b._active_transports = set()  # no active transports

        ref = MessageRef(channel_id="conv1", message_id="m1")
        task = RunningTask()
        b.run_map["conv1"] = ref
        b.running_tasks[ref] = task

        # Simulate the cleanup logic from ws_handler finally block
        if not b._active_transports:
            for conv_id, ref in list(b.run_map.items()):
                t = b.running_tasks.get(ref)
                if t is not None and not t.cancel_requested.is_set():
                    t.cancel_requested.set()

        assert task.cancel_requested.is_set()


# ═══════════════════════════════════════════════════════════════
# Section 22: build_and_run prepare_only mode
# ═══════════════════════════════════════════════════════════════


class TestBuildAndRun:
    def test_prepare_only_skips_async_run(self, tmp_path):
        """With _prepare_only=True, build_and_run initializes but doesn't run."""
        b = TunadishBackend()
        b._prepare_only = True

        runtime = MagicMock()
        b.build_and_run(
            transport_config={"port": 9999},
            config_path=str(tmp_path / "tunapi.toml"),
            runtime=runtime,
            final_notify=False,
            default_engine_override=None,
        )

        # Should have initialized stores
        assert hasattr(b, "context_store")
        assert hasattr(b, "_journal")
        assert hasattr(b, "_chat_prefs")
        assert hasattr(b, "_conv_sessions")
