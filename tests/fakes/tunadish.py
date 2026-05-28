from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from tunapi.context import RunContext


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
