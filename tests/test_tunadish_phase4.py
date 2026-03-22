"""Tests for tunadish backend Phase 4 RPC handlers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import anyio
import pytest

from tunapi.core.memory_facade import ProjectMemoryFacade
from tunapi.tunadish.backend import TunadishBackend
from tunapi.tunadish.transport import TunadishTransport

pytestmark = pytest.mark.anyio


# -- Fakes --


class FakeWs:
    """Captures messages sent via websocket."""

    def __init__(self):
        self.sent: list[dict[str, Any]] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def last(self) -> dict[str, Any]:
        return self.sent[-1]

    def last_params(self) -> dict[str, Any]:
        return self.last()["params"]


class FakeRuntime:
    def available_engine_ids(self) -> list[str]:
        return ["claude", "gemini"]


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
    return b


# -- discussion.save_roundtable --


class TestDiscussionSave:
    async def test_save_roundtable(self, backend, transport, ws):
        params = {
            "project": "proj",
            "discussion_id": "disc-1",
            "topic": "API design",
            "participants": ["claude", "gemini"],
            "rounds": 1,
            "transcript": [["claude", "Use REST"], ["gemini", "Agree"]],
        }
        await backend._handle_discussion_save(params, transport)
        result = ws.last_params()
        assert result["discussion_id"] == "disc-1"
        assert result["project"] == "proj"
        assert result["topic"] == "API design"

    async def test_save_requires_project(self, backend, transport, ws):
        await backend._handle_discussion_save({}, transport)
        assert ws.last_params()["error"] == "project required"

    async def test_save_with_branch_link(self, backend, transport, ws):
        # Create a branch first
        await backend._facade.branches.create_branch("proj", "feature/x")
        params = {
            "project": "proj",
            "discussion_id": "disc-2",
            "topic": "Branch test",
            "participants": ["claude"],
            "rounds": 1,
            "transcript": [],
            "branch_name": "feature/x",
        }
        await backend._handle_discussion_save(params, transport)
        result = ws.last_params()
        assert result["discussion_id"] == "disc-2"

        # Verify bidirectional link
        record = await backend._facade.discussions.get_record("proj", "disc-2")
        assert record.branch_name == "feature/x"


# -- discussion.link_branch --


class TestDiscussionLinkBranch:
    async def test_link_branch(self, backend, transport, ws):
        # Setup: create discussion and branch
        await backend._facade.discussions.create_record(
            "proj", discussion_id="d1", topic="T", participants=["claude"],
            rounds=1, transcript=[],
        )
        await backend._facade.branches.create_branch("proj", "main")

        params = {"project": "proj", "discussion_id": "d1", "branch_name": "main"}
        await backend._handle_discussion_link_branch(params, transport)
        result = ws.last_params()
        assert result["ok"] is True

    async def test_link_branch_missing_params(self, backend, transport, ws):
        await backend._handle_discussion_link_branch({"project": "p"}, transport)
        assert "error" in ws.last_params()


# -- synthesis.create_from_discussion --


class TestSynthesisCreate:
    async def test_create_synthesis(self, backend, transport, ws):
        await backend._facade.discussions.create_record(
            "proj", discussion_id="d1", topic="Design",
            participants=["claude"], rounds=1, transcript=[],
            summary="Use REST API",
        )
        params = {"project": "proj", "discussion_id": "d1"}
        await backend._handle_synthesis_create(params, transport)
        result = ws.last_params()
        assert "artifact_id" in result
        assert result["source_id"] == "d1"

    async def test_create_synthesis_not_found(self, backend, transport, ws):
        params = {"project": "proj", "discussion_id": "nonexistent"}
        await backend._handle_synthesis_create(params, transport)
        assert ws.last_params()["error"] == "discussion not found"

    async def test_create_synthesis_missing_params(self, backend, transport, ws):
        await backend._handle_synthesis_create({"project": ""}, transport)
        assert "error" in ws.last_params()


# -- review.request --


class TestReviewRequest:
    async def test_request_review(self, backend, transport, ws):
        # Create discussion + synthesis first
        await backend._facade.discussions.create_record(
            "proj", discussion_id="d1", topic="Design",
            participants=["claude"], rounds=1, transcript=[],
            summary="REST",
        )
        artifact = await backend._facade.save_synthesis_from_discussion("proj", "d1")

        params = {"project": "proj", "artifact_id": artifact.artifact_id}
        await backend._handle_review_request(params, transport)
        result = ws.last_params()
        assert "review_id" in result
        assert result["artifact_id"] == artifact.artifact_id

    async def test_request_review_not_found(self, backend, transport, ws):
        params = {"project": "proj", "artifact_id": "nonexistent"}
        await backend._handle_review_request(params, transport)
        assert ws.last_params()["error"] == "artifact not found"

    async def test_request_review_missing_params(self, backend, transport, ws):
        await backend._handle_review_request({}, transport)
        assert "error" in ws.last_params()


# -- handoff.create --


class TestHandoffCreate:
    async def test_create_handoff(self, backend, transport, ws):
        runtime = FakeRuntime()
        params = {"project": "proj", "session_id": "s1", "branch_id": "b1"}
        await backend._handle_handoff_create(params, runtime, transport)
        result = ws.last_params()
        assert result["project"] == "proj"
        assert "tunapi://open?" in result["uri"]
        assert "session=s1" in result["uri"]

    async def test_create_handoff_missing_project(self, backend, transport, ws):
        runtime = FakeRuntime()
        await backend._handle_handoff_create({}, runtime, transport)
        assert ws.last_params()["error"] == "project required"


# -- handoff.parse --


class TestHandoffParse:
    async def test_parse_handoff(self, backend, transport, ws):
        params = {"uri": "tunapi://open?project=myproj&session=s1&branch=b1"}
        await backend._handle_handoff_parse(params, transport)
        result = ws.last_params()
        assert result["project"] == "myproj"
        assert result["session_id"] == "s1"
        assert result["branch_id"] == "b1"

    async def test_parse_invalid_uri(self, backend, transport, ws):
        params = {"uri": "https://example.com"}
        await backend._handle_handoff_parse(params, transport)
        assert ws.last_params()["error"] == "invalid handoff URI"

    async def test_parse_empty_uri(self, backend, transport, ws):
        await backend._handle_handoff_parse({}, transport)
        assert ws.last_params()["error"] == "uri required"


# -- engine.list --


class TestEngineList:
    async def test_engine_list(self, backend, transport, ws):
        runtime = FakeRuntime()
        await backend._handle_engine_list(runtime, transport)
        result = ws.last_params()
        assert "engines" in result
        assert "claude" in result["engines"]
        assert "gemini" in result["engines"]
