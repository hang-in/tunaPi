"""State containers and pure helpers for the Discord event loop."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio
import discord

from tunapi.model import ResumeToken

if TYPE_CHECKING:
    from tunapi.context import RunContext

__all__ = [
    "MediaGroupBuffer",
    "ResumeDecision",
    "_MediaGroupState",
    "_MediaItem",
    "_diff_keys",
    "_extract_engine_id_from_header",
    "_strip_ctx_lines",
]


def _diff_keys(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Return sorted list of keys that differ between two dicts."""
    keys = set(old) | set(new)
    return sorted(key for key in keys if old.get(key) != new.get(key))


@dataclass(frozen=True, slots=True)
class ResumeDecision:
    resume_token: ResumeToken | None
    handled_by_running_task: bool


@dataclass(frozen=True, slots=True)
class _MediaItem:
    message: discord.Message
    prompt: str


@dataclass(slots=True)
class _MediaGroupState:
    token: int = 0
    items: list[_MediaItem] = field(default_factory=list)
    guild_id: int | None = None
    channel_id: int | None = None
    author_id: int | None = None
    thread_id: int | None = None
    job_channel_id: int | None = None
    engine_id: str | None = None
    resume_token: ResumeToken | None = None
    context: RunContext | None = None


class MediaGroupBuffer:
    def __init__(
        self,
        *,
        task_group,
        debounce_s: float,
        dispatch: Callable[[_MediaGroupState], Awaitable[None]],
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self._task_group = task_group
        self._debounce_s = float(debounce_s)
        self._dispatch = dispatch
        self._sleep = sleep
        self._groups: dict[tuple[int, int], _MediaGroupState] = {}

    def has_pending(self, *, channel_id: int, author_id: int) -> bool:
        return (channel_id, author_id) in self._groups

    def add(
        self,
        message: discord.Message,
        *,
        prompt: str,
        guild_id: int,
        channel_id: int,
        thread_id: int | None,
        job_channel_id: int,
        engine_id: str,
        resume_token: ResumeToken | None,
        context: RunContext | None,
    ) -> None:
        author_id = getattr(message.author, "id", None)
        if not isinstance(author_id, int):
            return
        key = (job_channel_id, author_id)
        state = self._groups.get(key)
        if state is None:
            state = _MediaGroupState()
            self._groups[key] = state
            self._task_group.start_soon(self._flush, key)
        state.items.append(_MediaItem(message=message, prompt=prompt))
        state.token += 1
        state.guild_id = guild_id
        state.channel_id = channel_id
        state.author_id = author_id
        state.thread_id = thread_id
        state.job_channel_id = job_channel_id
        state.engine_id = engine_id
        state.resume_token = resume_token
        state.context = context

    async def _flush(self, key: tuple[int, int]) -> None:
        while True:
            state = self._groups.get(key)
            if state is None:
                return
            token = state.token
            await self._sleep(self._debounce_s)
            state = self._groups.get(key)
            if state is None:
                return
            if state.token != token:
                continue
            del self._groups[key]
            await self._dispatch(state)
            return


def _extract_engine_id_from_header(text: str | None) -> str | None:
    """Extract engine id from a tunapi status header line.

    Header lines look like: "done · codex · 10s" (optionally with more parts).
    """
    if not text:
        return None
    first_line = text.splitlines()[0].strip()
    if not first_line:
        return None
    if " · " in first_line:
        parts = first_line.split(" · ")
    elif "·" in first_line:
        parts = [part.strip() for part in first_line.split("·")]
    else:
        return None
    if len(parts) < 2:
        return None
    engine_id = parts[1].strip().strip("`")
    return engine_id or None


def _strip_ctx_lines(text: str | None) -> str | None:
    """Strip tunapi context lines from bot messages.

    Discord reply-to-continue needs the resume token in the referenced message, but
    we don't want to couple branching to context-line parsing (which can raise if
    config changes). Removing `ctx:` lines keeps resume extraction reliable.
    """
    if not text:
        return None
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("`") and stripped.endswith("`") and len(stripped) > 1:
            stripped = stripped[1:-1].strip()
        if stripped.lower().startswith("ctx:"):
            continue
        lines.append(line)
    return "\n".join(lines).strip() or None
