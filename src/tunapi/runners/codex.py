from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import msgspec

from ..backends import EngineBackend, EngineConfig
from ..config import ConfigError
from ..events import EventFactory
from ..logging import get_logger
from ..model import ActionPhase, EngineId, ResumeToken, TunapiEvent
from ..runner import JsonlSubprocessRunner, MsgspecJsonlRunnerMixin, ResumeTokenMixin, Runner
from .codex_events import (  # noqa: F401  — re-exported for backward compat
    _format_change_summary,
    _normalize_change_list,
    _short_tool_name,
    _summarize_todo_list,
    _summarize_tool_result,
    _todo_title,
    translate_codex_event,
)
from .run_options import get_run_options
from ..schemas import codex as codex_schema

logger = get_logger(__name__)

ENGINE: EngineId = "codex"

__all__ = [
    "ENGINE",
    "CodexRunner",
    "find_exec_only_flag",
    "translate_codex_event",
]

_RESUME_RE = re.compile(r"(?im)^\s*`?codex\s+resume\s+(?P<token>[^`\s]+)`?\s*$")
_RECONNECTING_RE = re.compile(
    r"^Reconnecting\.{3}\s*(?P<attempt>\d+)/(?P<max>\d+)\s*$",
    re.IGNORECASE,
)
_EXEC_ONLY_FLAGS = {
    "--skip-git-repo-check",
    "--json",
    "--output-schema",
    "--output-last-message",
    "--color",
    "-o",
}
_EXEC_ONLY_PREFIXES = (
    "--output-schema=",
    "--output-last-message=",
    "--color=",
)


def find_exec_only_flag(extra_args: list[str]) -> str | None:
    for arg in extra_args:
        if arg in _EXEC_ONLY_FLAGS:
            return arg
        for prefix in _EXEC_ONLY_PREFIXES:
            if arg.startswith(prefix):
                return arg
    return None


def _parse_reconnect_message(message: str) -> tuple[int, int] | None:
    match = _RECONNECTING_RE.match(message)
    if not match:
        return None
    try:
        attempt = int(match.group("attempt"))
        max_attempts = int(match.group("max"))
    except (TypeError, ValueError):
        return None
    return (attempt, max_attempts)


@dataclass(frozen=True, slots=True)
class _AgentMessageSummary:
    text: str
    phase: str | None


def _select_final_answer(agent_messages: list[_AgentMessageSummary]) -> str | None:
    for message in reversed(agent_messages):
        if message.phase == "final_answer":
            return message.text
    for message in reversed(agent_messages):
        if message.phase in {None, ""}:
            return message.text
    return None


@dataclass(slots=True)
class CodexRunState:
    factory: EventFactory
    note_seq: int = 0
    final_answer: str | None = None
    turn_agent_messages: list[_AgentMessageSummary] = field(default_factory=list)
    turn_index: int = 0


class CodexRunner(MsgspecJsonlRunnerMixin, ResumeTokenMixin, JsonlSubprocessRunner):
    engine: EngineId = ENGINE
    resume_re = _RESUME_RE
    logger = logger

    def __init__(
        self,
        *,
        codex_cmd: str,
        codex_script: str | None = None,
        extra_args: list[str],
        title: str = "Codex",
    ) -> None:
        self.codex_cmd = codex_cmd
        self.codex_script = codex_script
        self.extra_args = extra_args
        self.session_title = title

    def command(self) -> str:
        return self.codex_cmd

    def build_args(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> list[str]:
        run_options = get_run_options()
        args: list[str] = []
        if self.codex_script:
            args.append(self.codex_script)
        args.extend(self.extra_args)
        if run_options is not None:
            if run_options.model:
                args.extend(["--model", str(run_options.model)])
            if run_options.reasoning:
                args.extend(
                    [
                        "-c",
                        f"model_reasoning_effort={run_options.reasoning}",
                    ]
                )
        args.extend(
            [
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--color=never",
            ]
        )
        if resume:
            args.extend(["resume", resume.value, "-"])
        else:
            args.append("-")
        return args

    def new_state(self, prompt: str, resume: ResumeToken | None) -> CodexRunState:
        return CodexRunState(factory=EventFactory(ENGINE))

    def start_run(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: CodexRunState,
    ) -> None:
        pass

    def decode_jsonl(self, *, line: bytes) -> codex_schema.ThreadEvent:
        return codex_schema.decode_event(line)

    def pipes_error_message(self) -> str:
        return "codex exec failed to open subprocess pipes"

    def translate(
        self,
        data: codex_schema.ThreadEvent,
        *,
        state: CodexRunState,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> list[TunapiEvent]:
        factory = state.factory
        match data:
            case codex_schema.StreamError(message=message):
                reconnect = _parse_reconnect_message(message)
                if reconnect is not None:
                    attempt, max_attempts = reconnect
                    phase: ActionPhase = "started" if attempt <= 1 else "updated"
                    return [
                        factory.action(
                            phase=phase,
                            action_id="codex.reconnect",
                            kind="note",
                            title=message,
                            detail={"attempt": attempt, "max": max_attempts},
                            level="info",
                        )
                    ]
                return [self.note_event(message, state=state, ok=False)]
            case codex_schema.TurnFailed(error=error):
                resume_for_completed = found_session or resume
                return [
                    factory.completed_error(
                        error=error.message,
                        answer=state.final_answer or "",
                        resume=resume_for_completed,
                    )
                ]
            case codex_schema.TurnStarted():
                action_id = f"turn_{state.turn_index}"
                state.turn_index += 1
                state.final_answer = None
                state.turn_agent_messages.clear()
                return [
                    factory.action_started(
                        action_id=action_id,
                        kind="turn",
                        title="turn started",
                    )
                ]
            case codex_schema.TurnCompleted(usage=usage):
                resume_for_completed = found_session or resume
                return [
                    factory.completed_ok(
                        answer=state.final_answer or "",
                        resume=resume_for_completed,
                        usage=msgspec.to_builtins(usage),
                    )
                ]
            case codex_schema.ItemCompleted(
                item=codex_schema.AgentMessageItem(text=text, phase=message_phase)
            ):
                state.turn_agent_messages.append(
                    _AgentMessageSummary(text=text, phase=message_phase)
                )
                selected = _select_final_answer(state.turn_agent_messages)
                if selected is not None:
                    state.final_answer = selected
                if len(state.turn_agent_messages) > 1:
                    logger.debug("codex.multiple_agent_messages")
            case _:
                pass

        return translate_codex_event(
            data,
            title=self.session_title,
            factory=factory,
        )

    def process_error_events(
        self,
        rc: int,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: CodexRunState,
    ) -> list[TunapiEvent]:
        message = f"codex exec failed (rc={rc})."
        resume_for_completed = found_session or resume
        return [
            self.note_event(
                message,
                state=state,
                ok=False,
            ),
            state.factory.completed_error(
                error=message,
                answer=state.final_answer or "",
                resume=resume_for_completed,
            ),
        ]

    def stream_end_events(
        self,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: CodexRunState,
    ) -> list[TunapiEvent]:
        if not found_session:
            message = "codex exec finished but no session_id/thread_id was captured"
            resume_for_completed = resume
            return [
                state.factory.completed_error(
                    error=message,
                    answer=state.final_answer or "",
                    resume=resume_for_completed,
                )
            ]
        logger.info("codex.session.completed", resume=found_session.value)
        return [
            state.factory.completed_ok(
                answer=state.final_answer or "",
                resume=found_session,
            )
        ]


def build_runner(config: EngineConfig, config_path: Path) -> Runner:
    codex_script: str | None = None
    if os.name == "nt":
        npm_root = Path.home() / "AppData" / "Roaming" / "npm"
        entry = npm_root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if entry.exists():
            codex_cmd = shutil.which("node") or "node"
            codex_script = str(entry)
        else:
            codex_cmd = shutil.which("codex") or "codex"
    else:
        codex_cmd = shutil.which("codex") or "codex"

    extra_args_value = config.get("extra_args")
    if extra_args_value is None:
        extra_args = ["-c", "notify=[]"]
    elif isinstance(extra_args_value, list) and all(
        isinstance(item, str) for item in extra_args_value
    ):
        extra_args = list(extra_args_value)
    else:
        raise ConfigError(
            f"Invalid `codex.extra_args` in {config_path}; expected a list of strings."
        )

    exec_only_flag = find_exec_only_flag(extra_args)
    if exec_only_flag:
        raise ConfigError(
            f"Invalid `codex.extra_args` in {config_path}; exec-only flag "
            f"{exec_only_flag!r} is managed by Tunapi."
        )

    title = "Codex"
    profile_value = config.get("profile")
    if profile_value:
        if not isinstance(profile_value, str):
            raise ConfigError(
                f"Invalid `codex.profile` in {config_path}; expected a string."
            )
        extra_args.extend(["--profile", profile_value])
        title = profile_value

    return CodexRunner(codex_cmd=codex_cmd, codex_script=codex_script, extra_args=extra_args, title=title)


BACKEND = EngineBackend(
    id="codex",
    build_runner=build_runner,
    install_cmd="npm install -g @openai/codex",
)
