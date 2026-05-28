"""Presenter logic shared by Slack and Mattermost.

Both transports render :class:`ProgressState` identically — the only
difference is the final text-preparation function that adapts Markdown
to the platform's dialect.  This module provides the shared logic;
each transport injects its own ``prepare`` and ``prepare_multi``
callables.

Telegram is excluded because it uses inline-keyboard cancel, entity
rendering, and different split/followup semantics.
"""

from __future__ import annotations

from collections.abc import Callable

from ..markdown import MarkdownFormatter, MarkdownParts
from ..progress import ProgressState
from ..transport import RenderedMessage


class ChatPresenter:
    """Renders :class:`ProgressState` into transport messages.

    *prepare* converts Markdown ``parts`` → single ``str``.
    *prepare_multi* converts Markdown ``parts`` → ``list[str]`` (for
    overflow=split mode).
    """

    def __init__(
        self,
        *,
        prepare: Callable[[MarkdownParts], str],
        prepare_multi: Callable[[MarkdownParts], list[str]],
        formatter: MarkdownFormatter | None = None,
        message_overflow: str = "trim",
        show_resume_line: bool = True,
    ) -> None:
        self._prepare = prepare
        self._prepare_multi = prepare_multi
        self._formatter = formatter or MarkdownFormatter()
        self._message_overflow = message_overflow
        self._show_resume_line = show_resume_line

    def _strip_resume(self, state: ProgressState) -> ProgressState:
        """Return a copy of state with resume_line cleared when hidden."""
        if self._show_resume_line or not state.resume_line:
            return state
        return ProgressState(
            engine=state.engine,
            action_count=state.action_count,
            actions=state.actions,
            resume=state.resume,
            resume_line=None,
            context_line=state.context_line,
        )

    def render_progress(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        label: str = "working",
    ) -> RenderedMessage:
        state = self._strip_resume(state)
        parts = self._formatter.render_progress_parts(
            state, elapsed_s=elapsed_s, label=label
        )
        text = self._prepare(parts)
        return RenderedMessage(text=text)

    def render_final(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        status: str,
        answer: str,
    ) -> RenderedMessage:
        state = self._strip_resume(state)
        parts = self._formatter.render_final_parts(
            state, elapsed_s=elapsed_s, status=status, answer=answer
        )

        if self._message_overflow == "split":
            messages = self._prepare_multi(parts)
            if len(messages) > 1:
                followups = [RenderedMessage(text=msg) for msg in messages[1:]]
                return RenderedMessage(
                    text=messages[0],
                    extra={"followups": followups},
                )
            return RenderedMessage(text=messages[0])

        text = self._prepare(parts)
        return RenderedMessage(text=text)

    def render_progress_summary(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        label: str = "done",
        max_actions: int = 3,
    ) -> RenderedMessage:
        """Render a compact progress summary (last N actions)."""
        state = self._strip_resume(state)
        parts = self._formatter.render_summary_parts(
            state, elapsed_s=elapsed_s, label=label, max_actions=max_actions
        )
        text = self._prepare(parts)
        return RenderedMessage(text=text)
