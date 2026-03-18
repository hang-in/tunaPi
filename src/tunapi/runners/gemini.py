"""Gemini CLI runner for tunapi."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import msgspec

from ..backends import EngineBackend, EngineConfig
from ..events import EventFactory
from ..logging import get_logger
from ..model import Action, ActionKind, ResumeToken, TunapiEvent
from ..runner import JsonlSubprocessRunner, ResumeTokenMixin, Runner
from ..schemas import gemini as gemini_schema

logger = get_logger(__name__)

ENGINE = "gemini"

_RESUME_RE = re.compile(
    r"(?im)^\s*`?gemini\s+(?:--resume|-r)\s+(?P<token>[^`\s]+)`?\s*$"
)


@dataclass(slots=True)
class GeminiStreamState:
    factory: EventFactory = field(default_factory=lambda: EventFactory(ENGINE))
    pending_actions: dict[str, Action] = field(default_factory=dict)
    last_assistant_text: str = ""
    session_id: str = ""


def _tool_kind(name: str) -> ActionKind:
    name_lower = name.lower()
    if any(k in name_lower for k in ("read", "write", "edit", "list_directory")):
        return "file_change"
    if any(k in name_lower for k in ("bash", "shell", "exec", "run")):
        return "command"
    if "search" in name_lower:
        return "web_search"
    return "tool"


def translate_gemini_event(
    event: gemini_schema.GeminiStreamEvent,
    *,
    state: GeminiStreamState,
) -> list[TunapiEvent]:
    factory = state.factory

    match event:
        case gemini_schema.GeminiInitEvent():
            state.session_id = event.session_id
            model = event.model or "gemini"
            token = ResumeToken(engine=ENGINE, value=event.session_id)
            return [factory.started(token, title=model)]

        case gemini_schema.GeminiMessageEvent(role="assistant"):
            if event.content:
                state.last_assistant_text += event.content
            return []

        case gemini_schema.GeminiToolUseEvent():
            kind = _tool_kind(event.tool_name)
            title = event.tool_name
            # Extract path from parameters for file tools
            params = event.parameters
            detail: dict[str, Any] = {"name": event.tool_name, "input": params}
            if kind == "file_change":
                path = params.get("file_path") or params.get("path") or params.get("dir_path")
                if isinstance(path, str):
                    title = path
                    detail["changes"] = [{"path": path, "kind": "update"}]

            action = Action(id=event.tool_id, kind=kind, title=title, detail=detail)
            state.pending_actions[event.tool_id] = action
            return [factory.action_started(
                action_id=action.id, kind=action.kind,
                title=action.title, detail=action.detail,
            )]

        case gemini_schema.GeminiToolResultEvent():
            action = state.pending_actions.pop(event.tool_id, None)
            if action is None:
                action = Action(id=event.tool_id, kind="tool", title="tool", detail={})
            ok = event.status == "success"
            return [factory.action_completed(
                action_id=action.id, kind=action.kind,
                title=action.title, ok=ok,
                detail=action.detail | {"result_preview": event.output[:200]},
            )]

        case gemini_schema.GeminiResultEvent():
            ok = event.status == "success"
            answer = state.last_assistant_text.strip()
            resume = ResumeToken(engine=ENGINE, value=state.session_id)
            usage: dict[str, Any] = {}
            stats = event.stats
            if stats.duration_ms:
                usage["duration_ms"] = stats.duration_ms
            if stats.total_tokens:
                usage["total_tokens"] = stats.total_tokens
                usage["input_tokens"] = stats.input_tokens
                usage["output_tokens"] = stats.output_tokens
            return [factory.completed(
                ok=ok, answer=answer, resume=resume,
                error=None if ok else "gemini run failed",
                usage=usage or None,
            )]

        case _:
            return []


@dataclass(slots=True)
class GeminiRunner(ResumeTokenMixin, JsonlSubprocessRunner):
    engine: str = ENGINE
    resume_re: re.Pattern[str] = _RESUME_RE

    gemini_cmd: str = "gemini"
    model: str | None = None
    session_title: str = "gemini"
    logger = logger

    def format_resume(self, token: ResumeToken) -> str:
        return f"`gemini --resume {token.value}`"

    def command(self) -> str:
        return self.gemini_cmd

    def build_args(
        self, prompt: str, resume: ResumeToken | None, *, state: Any
    ) -> list[str]:
        args: list[str] = ["-p", prompt, "--output-format", "stream-json", "-y"]
        if resume is not None:
            args.extend(["--resume", resume.value])
        if self.model is not None:
            args.extend(["--model", self.model])
        return args

    def stdin_payload(
        self, prompt: str, resume: ResumeToken | None, *, state: Any
    ) -> bytes | None:
        return None

    def env(self, *, state: Any) -> dict[str, str] | None:
        return None

    def new_state(
        self, prompt: str, resume: ResumeToken | None
    ) -> GeminiStreamState:
        return GeminiStreamState()

    def start_run(
        self, prompt: str, resume: ResumeToken | None, *, state: GeminiStreamState
    ) -> None:
        pass

    def decode_jsonl(self, *, line: bytes) -> gemini_schema.GeminiStreamEvent:
        return gemini_schema.decode_stream_json_line(line)

    def decode_error_events(
        self, *, raw: str, line: str, error: Exception, state: GeminiStreamState
    ) -> list[TunapiEvent]:
        if isinstance(error, msgspec.DecodeError):
            self.get_logger().warning(
                "jsonl.msgspec.invalid",
                tag=self.tag(),
                error=str(error),
            )
            return []
        return super().decode_error_events(raw=raw, line=line, error=error, state=state)

    def invalid_json_events(
        self, *, raw: str, line: str, state: GeminiStreamState
    ) -> list[TunapiEvent]:
        return []

    def translate(
        self,
        data: gemini_schema.GeminiStreamEvent,
        *,
        state: GeminiStreamState,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> list[TunapiEvent]:
        return translate_gemini_event(data, state=state)

    def process_error_events(
        self, rc: int, *, resume: ResumeToken | None,
        found_session: ResumeToken | None, state: GeminiStreamState,
    ) -> list[TunapiEvent]:
        message = f"gemini failed (rc={rc})."
        return [
            self.note_event(message, state=state, ok=False),
            state.factory.completed_error(
                error=message, resume=found_session or resume,
            ),
        ]

    def stream_end_events(
        self, *, resume: ResumeToken | None,
        found_session: ResumeToken | None, state: GeminiStreamState,
    ) -> list[TunapiEvent]:
        if not found_session:
            return [state.factory.completed_error(
                error="gemini finished but no session_id was captured",
                resume=resume,
            )]
        return [state.factory.completed_error(
            error="gemini finished without a result event",
            answer=state.last_assistant_text or "",
            resume=found_session,
        )]


def build_runner(config: EngineConfig, _config_path: Path) -> Runner:
    gemini_cmd = shutil.which("gemini") or "gemini"
    model = config.get("model")
    title = str(model) if model else "gemini"
    return GeminiRunner(gemini_cmd=gemini_cmd, model=model, session_title=title)


BACKEND = EngineBackend(
    id="gemini",
    build_runner=build_runner,
    cli_cmd="gemini",
    install_cmd="npm install -g @anthropic-ai/gemini-cli",
)
