"""Tests for tunadish context handlers (targeting context_handlers.py)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tunapi.context import RunContext
from tunapi.tunadish.context_handlers import (
    handle_project_context,
    handle_branch_list_json,
    handle_memory_list_json,
    handle_review_list_json,
    handle_project_list,
    handle_conversation_create,
    handle_conversation_delete,
    handle_conversation_list,
    handle_conversation_history,
)
from tunapi.tunadish.transport import TunadishTransport
from .fakes.tunadish import FakeWs, FakeRuntime

pytestmark = pytest.mark.anyio


@pytest.fixture
def ws():
    return FakeWs()


@pytest.fixture
def transport(ws):
    return TunadishTransport(ws)


@pytest.fixture
def runtime():
    return FakeRuntime()


@pytest.fixture
def backend():
    b = MagicMock()
    # Mock context_store
    b.context_store = MagicMock()
    b.context_store.get_context = AsyncMock(return_value=None)
    b.context_store.set_context = AsyncMock()
    b.context_store.clear = AsyncMock()
    b.context_store.list_conversations = MagicMock(return_value=[])
    b.context_store.get_conv_settings = MagicMock()

    # Mock chat_prefs
    b._chat_prefs = MagicMock()
    b._chat_prefs.get_default_engine = AsyncMock(return_value=None)
    b._chat_prefs.get_engine_model = AsyncMock(return_value=None)
    b._chat_prefs.get_trigger_mode = AsyncMock(return_value=None)

    # Mock conv_sessions & project_sessions
    b._conv_sessions = MagicMock()
    b._conv_sessions.get = AsyncMock(return_value=None)
    b._project_sessions = MagicMock()
    b._project_sessions.get = AsyncMock(return_value=None)

    # Mock facade
    b._facade = MagicMock()
    b._facade.get_project_context_dto = AsyncMock()
    b._facade.conv_branches.list = AsyncMock(return_value=[])
    b._facade.branches.list_branches = AsyncMock(return_value=[])
    b._facade.memory.list_entries = AsyncMock(return_value=[])
    b._facade.reviews.list = AsyncMock(return_value=[])

    # Mock discover projects
    b._discover_projects = MagicMock(return_value=[])

    # Mock journals
    b._journal = MagicMock()
    b._cross_journals = []

    return b


# ═══════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════


class TestProjectContext:
    async def test_no_project(self, backend, transport, runtime, ws):
        await handle_project_context(backend, {}, runtime, transport)
        assert ws.last_params()["error"] == "no project"

    async def test_project_context_happy_path(
        self, backend, transport, runtime, ws, tmp_path
    ):
        # 1. Setup mock run context and params
        backend.context_store.get_context.return_value = RunContext(project="myproj")
        backend._chat_prefs.get_default_engine.return_value = "claude"
        backend._chat_prefs.get_engine_model.return_value = "claude-sonnet"
        backend._chat_prefs.get_trigger_mode.return_value = "mentions"

        # DTO setup
        dto = MagicMock()
        dto.memory_entries = [
            MagicMock(
                id="m1",
                type="memo",
                title="title1",
                content="content1" * 50,
                source="s1",
                tags={"t1"},
                timestamp="123",
            )
        ]
        dto.active_branches = [
            MagicMock(
                branch_name="feat",
                description="desc",
                status="active",
                discussion_ids=["d1", "d2"],
            )
        ]
        dto.discussions = [
            MagicMock(
                discussion_id="d1",
                topic="topic1",
                status="open",
                participants={"user1"},
            )
        ]
        dto.pending_reviews = ["r1"]
        dto.markdown = "# hello"
        backend._facade.get_project_context_dto.return_value = dto

        cb = MagicMock(
            branch_id="b1",
            label="b1_lbl",
            status="active",
            git_branch="feat",
            parent_branch_id=None,
            session_id="s1",
            checkpoint_id="c1",
        )
        backend._facade.conv_branches.list.return_value = [cb]

        conv_settings_mock = MagicMock()
        conv_settings_mock.to_dict.return_value = {"engine": "claude"}
        backend.context_store.get_conv_settings.return_value = conv_settings_mock

        # Setup runtime
        runtime.default_engine = "claude"
        runtime.resolve_run_cwd = MagicMock(return_value=tmp_path)

        # Mock git process to return main branch
        async def mock_git(*args, **kwargs):
            proc = MagicMock()
            proc.returncode = 0

            async def communicate():
                return b"main\n", b""

            proc.communicate = communicate
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_git):
            await handle_project_context(
                backend, {"conversation_id": "conv1"}, runtime, transport
            )

        params = ws.last_params()
        assert params["project"] == "myproj"
        assert params["engine"] == "claude"
        assert params["model"] == "claude-sonnet"
        assert params["git_branch"] == "main"
        assert params["markdown"] == "# hello"
        assert len(params["memory_entries"]) == 1
        assert params["memory_entries"][0]["content"] == ("content1" * 50)[:200]
        assert params["conv_branches"][0]["id"] == "b1"
        assert params["conv_settings"] == {"engine": "claude"}

    async def test_project_context_fallback_model_and_git_fail(
        self, backend, transport, runtime, ws
    ):
        # Trigger model fallback and git subprocess exception
        backend.context_store.get_context.return_value = None
        backend._chat_prefs.get_default_engine.return_value = "claude"
        backend._chat_prefs.get_engine_model.return_value = None

        dto = MagicMock()
        dto.memory_entries = []
        dto.active_branches = []
        dto.discussions = []
        dto.pending_reviews = []
        dto.markdown = ""
        backend._facade.get_project_context_dto.return_value = dto
        backend.context_store.get_conv_settings.return_value.to_dict.return_value = {}

        # Set projects map in runtime
        pc = MagicMock()
        pc.path = Path("/nonexistent/path")
        runtime._projects.projects = {"myproj": pc}

        # Mock git process to raise exception
        async def mock_git(*args, **kwargs):
            raise OSError("git error")

        with patch("asyncio.create_subprocess_exec", side_effect=mock_git):
            await handle_project_context(
                backend,
                {"conversation_id": "conv2", "project": "myproj"},
                runtime,
                transport,
            )

        params = ws.last_params()
        assert params["project"] == "myproj"
        assert (
            params["model"] == "claude-sonnet-4-20250514"
        )  # Resolved from fake runner
        assert params["git_branch"] is None
        backend.context_store.set_context.assert_called_once()

    async def test_project_context_conv_session_token(
        self, backend, transport, runtime, ws
    ):
        backend.context_store.get_context.return_value = RunContext(project="myproj")

        # mock conv_session with token
        conv_session = MagicMock()
        conv_session.token = "conv_token_123"
        backend._conv_sessions.get.return_value = conv_session

        dto = MagicMock()
        dto.memory_entries = []
        dto.active_branches = []
        dto.discussions = []
        dto.pending_reviews = []
        dto.markdown = ""
        backend._facade.get_project_context_dto.return_value = dto
        backend.context_store.get_conv_settings.return_value.to_dict.return_value = {}

        await handle_project_context(
            backend,
            {"conversation_id": "conv123", "project": "myproj"},
            runtime,
            transport,
        )

        params = ws.last_params()
        assert params["resume_token"] == "conv_token_123"


class TestBranchListJson:
    async def test_no_project(self, backend, transport, runtime, ws):
        await handle_branch_list_json(backend, {}, runtime, transport)
        assert ws.last_params()["error"] == "no project"

    async def test_branch_list_happy_path(self, backend, transport, runtime, ws):
        backend.context_store.get_context.return_value = RunContext(project="myproj")

        gb = MagicMock(
            branch_name="main",
            status="active",
            description="desc",
            parent_branch=None,
            memory_entry_ids=["m1"],
            discussion_ids=["d1"],
        )
        cb = MagicMock(
            branch_id="b1",
            label="b1_lbl",
            status="active",
            git_branch="feat",
            parent_branch_id=None,
            session_id="s1",
            checkpoint_id="c1",
        )

        backend._facade.branches.list_branches.return_value = [gb]
        backend._facade.conv_branches.list.return_value = [cb]

        await handle_branch_list_json(backend, {}, runtime, transport)

        params = ws.last_params()
        assert params["project"] == "myproj"
        assert len(params["git_branches"]) == 1
        assert params["git_branches"][0]["name"] == "main"
        assert len(params["conv_branches"]) == 1
        assert params["conv_branches"][0]["id"] == "b1"


class TestMemoryListJson:
    async def test_no_project(self, backend, transport, ws):
        await handle_memory_list_json(backend, {}, transport)
        assert ws.last_params()["error"] == "no project"

    async def test_memory_list_happy_path(self, backend, transport, ws):
        backend.context_store.get_context.return_value = RunContext(project="myproj")
        e = MagicMock(
            id="m1",
            type="memo",
            title="title1",
            content="content1",
            source="s1",
            tags={"t1"},
            timestamp="123",
        )
        backend._facade.memory.list_entries.return_value = [e]

        await handle_memory_list_json(backend, {"type": "memo", "limit": 10}, transport)

        params = ws.last_params()
        assert params["project"] == "myproj"
        assert len(params["entries"]) == 1
        assert params["entries"][0]["id"] == "m1"


class TestReviewListJson:
    async def test_no_project(self, backend, transport, ws):
        await handle_review_list_json(backend, {}, transport)
        assert ws.last_params()["error"] == "no project"

    async def test_review_list_happy_path(self, backend, transport, ws):
        backend.context_store.get_context.return_value = RunContext(project="myproj")
        r = MagicMock(
            review_id="r1",
            artifact_id="art1",
            artifact_version="v1",
            status="pending",
            reviewer_comment="comment",
            created_at=456,
        )
        backend._facade.reviews.list.return_value = [r]

        await handle_review_list_json(backend, {"status": "pending"}, transport)

        params = ws.last_params()
        assert params["project"] == "myproj"
        assert len(params["reviews"]) == 1
        assert params["reviews"][0]["id"] == "r1"


class TestProjectList:
    async def test_project_list_happy_path(
        self, backend, transport, runtime, ws, tmp_path
    ):
        runtime._aliases = ["AliasProj"]

        pc1 = MagicMock()
        pc1.path = tmp_path / "proj1"
        pc1.path.mkdir(exist_ok=True)
        (pc1.path / ".git").mkdir()  # git directory -> type: "project"
        pc1.alias = "p1"
        pc1.default_engine = "claude"

        pc2 = MagicMock()
        pc2.path = tmp_path / "proj2"
        pc2.path.mkdir(
            exist_ok=True
        )  # no .git directory but has chat_id -> type: "channel"
        pc2.chat_id = "chat123"
        pc2.alias = "p2"
        pc2.default_engine = None

        runtime._projects.projects = {"proj1": pc1, "proj2": pc2}
        backend._discover_projects.return_value = [
            {"key": "disc", "path": "/path/to/disc"}
        ]

        await handle_project_list(backend, {}, runtime, transport)

        params = ws.last_params()
        configured = params["configured"]
        assert len(configured) == 3  # proj1, proj2 + AliasProj (alias_proj)

        c1 = next(c for c in configured if c["key"] == "proj1")
        assert c1["type"] == "project"
        assert c1["default_engine"] == "claude"

        c2 = next(c for c in configured if c["key"] == "proj2")
        assert c2["type"] == "channel"

        c3 = next(c for c in configured if c["key"] == "aliasproj")
        assert c3["alias"] == "AliasProj"

        assert params["discovered"] == [{"key": "disc", "path": "/path/to/disc"}]


class TestConversationCreateDelete:
    async def test_conversation_create(self, backend, transport, ws):
        await handle_conversation_create(
            backend,
            {"conversation_id": "c1", "project": "p1", "label": "lbl"},
            transport,
        )
        backend.context_store.set_context.assert_called_once()
        params = ws.last_params()
        assert params["conversation_id"] == "c1"
        assert params["project"] == "p1"
        assert params["label"] == "lbl"

    async def test_conversation_delete(self, backend, transport, ws, tmp_path):
        backend._journal._base_dir = tmp_path
        journal_file = tmp_path / "c1.jsonl"
        journal_file.write_text("{}")

        await handle_conversation_delete(backend, {"conversation_id": "c1"}, transport)
        backend.context_store.clear.assert_called_once_with("c1")
        assert not journal_file.exists()
        assert ws.last_params()["conversation_id"] == "c1"

    async def test_conversation_delete_no_journal(
        self, backend, transport, ws, tmp_path
    ):
        backend._journal._base_dir = tmp_path
        # Do not create journal file

        await handle_conversation_delete(backend, {"conversation_id": "c2"}, transport)
        backend.context_store.clear.assert_called_once_with("c2")
        assert ws.last_params()["conversation_id"] == "c2"


class TestConversationList:
    async def test_conversation_list_happy_path(
        self, backend, transport, runtime, ws, tmp_path
    ):
        backend.context_store.list_conversations.return_value = [
            {
                "id": "conv1",
                "project": "p1",
                "branch": None,
                "label": "conv1",
                "created_at": 1.0,
            }
        ]

        # Setup cross transport journal
        mock_journal = MagicMock()
        mock_journal._base_dir = tmp_path
        (tmp_path / "chat123.jsonl").write_text("{}")

        entry = MagicMock()
        entry.timestamp = "2026-05-28T12:00:00Z"
        mock_journal.recent_entries = AsyncMock(return_value=[entry])

        backend._cross_journals = [("slack", mock_journal)]
        runtime.chat_ids_for_project = MagicMock(return_value=["chat123"])

        await handle_conversation_list(backend, {"project": "p1"}, runtime, transport)

        params = ws.last_params()
        conversations = params["conversations"]
        assert len(conversations) == 2
        assert conversations[0]["id"] == "conv1"
        assert conversations[0]["source"] == "tunadish"
        assert conversations[1]["id"] == "chat123"
        assert conversations[1]["source"] == "slack"
        assert conversations[1]["last_activity"] == "2026-05-28T12:00:00Z"

    async def test_conversation_list_chat_ids_fail(
        self, backend, transport, runtime, ws
    ):
        backend.context_store.list_conversations.return_value = [
            {
                "id": "conv1",
                "project": "p1",
                "branch": None,
                "label": "conv1",
                "created_at": 1.0,
            }
        ]
        runtime.chat_ids_for_project = MagicMock(side_effect=Exception("oops"))

        await handle_conversation_list(backend, {"project": "p1"}, runtime, transport)

        params = ws.last_params()
        conversations = params["conversations"]
        assert len(conversations) == 1
        assert conversations[0]["id"] == "conv1"


class TestConversationHistory:
    async def test_conversation_history_happy_path(
        self, backend, transport, ws, tmp_path
    ):
        e1 = MagicMock(event="prompt", run_id="run1", engine="claude", timestamp=10.0)
        e1.data = {
            "text": "<relevant_code>some code</relevant_code> --- user prompt text",
            "model": "claude-sonnet",
        }

        e2 = MagicMock(event="completed", run_id="run1", timestamp=20.0)
        e2.data = {"ok": True, "answer": "assistant response"}

        backend._journal.recent_entries = AsyncMock(return_value=[e1, e2])

        await handle_conversation_history(backend, {"conversation_id": "c1"}, transport)

        params = ws.last_params()
        assert params["conversation_id"] == "c1"
        messages = params["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "user prompt text"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "assistant response"
        assert messages[1]["model"] == "claude-sonnet"
        assert messages[1]["engine"] == "claude"
