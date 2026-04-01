"""Message rendering for Discord."""

from __future__ import annotations

import re

from tunapi.markdown import MarkdownParts, assemble_markdown_parts

# Discord has a 2000 character limit per message
MAX_MESSAGE_CHARS = 2000
MAX_BODY_CHARS = 1500  # Leave room for header/footer

_FENCE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>[`~]{3,})(?P<info>.*)$")


class _FenceState:
    __slots__ = ("fence", "indent", "header")

    def __init__(self, fence: str, indent: str, header: str) -> None:
        self.fence = fence
        self.indent = indent
        self.header = header


def _update_fence_state(line: str, state: _FenceState | None) -> _FenceState | None:
    match = _FENCE_RE.match(line)
    if match is None:
        return state
    fence = match.group("fence")
    indent = match.group("indent")
    if state is None:
        return _FenceState(fence=fence, indent=indent, header=line)
    if fence[0] == state.fence[0] and len(fence) >= len(state.fence):
        return None
    return state


def _scan_fence_state(text: str, state: _FenceState | None) -> _FenceState | None:
    for line in text.splitlines():
        state = _update_fence_state(line, state)
    return state


def _ensure_trailing_newline(text: str) -> str:
    if text.endswith("\n") or text.endswith("\r"):
        return text
    return text + "\n"


def _close_fence_chunk(text: str, state: _FenceState) -> str:
    return _ensure_trailing_newline(text) + f"{state.indent}{state.fence}\n"


def _reopen_fence_prefix(state: _FenceState) -> str:
    return f"{state.header}\n"


def _split_long_line(line: str, max_chars: int) -> list[str]:
    if len(line) <= max_chars:
        return [line]
    # Split line preserving ending
    ending = ""
    if line.endswith("\r\n"):
        line, ending = line[:-2], "\r\n"
    elif line.endswith("\n"):
        line, ending = line[:-1], "\n"
    elif line.endswith("\r"):
        line, ending = line[:-1], "\r"

    parts: list[str] = []
    for idx in range(0, len(line), max_chars):
        chunk = line[idx : idx + max_chars]
        if idx + max_chars >= len(line):
            chunk += ending
        parts.append(chunk)
    return parts if parts else [ending] if ending else []


def _split_block(block: str, max_chars: int) -> list[str]:
    if len(block) <= max_chars:
        return [block]
    pieces: list[str] = []
    current = ""
    for line in block.splitlines(keepends=True):
        for part in _split_long_line(line, max_chars):
            if not part:
                continue
            if current and len(current) + len(part) > max_chars:
                pieces.append(current)
                current = ""
            current += part
            if len(current) == max_chars:
                pieces.append(current)
                current = ""
    if current:
        pieces.append(current)
    return pieces


def split_markdown_body(body: str, max_chars: int) -> list[str]:
    """Split markdown body into chunks respecting code fences."""
    if not body or not body.strip():
        return []
    max_chars = max(1, int(max_chars))
    segments = re.split(r"(\n{2,})", body)
    blocks: list[str] = []
    for idx in range(0, len(segments), 2):
        paragraph = segments[idx]
        separator = segments[idx + 1] if idx + 1 < len(segments) else ""
        block = paragraph + separator
        if block:
            blocks.append(block)

    chunks: list[str] = []
    current = ""
    state: _FenceState | None = None
    for block in blocks:
        for piece in _split_block(block, max_chars):
            if not current:
                current = piece
                state = _scan_fence_state(piece, state)
                continue
            if len(current) + len(piece) <= max_chars:
                current += piece
                state = _scan_fence_state(piece, state)
                continue

            if state is not None:
                current = _close_fence_chunk(current, state)
            chunks.append(current)
            current = _reopen_fence_prefix(state) if state is not None else ""
            current += piece
            state = _scan_fence_state(piece, state)

    if current:
        chunks.append(current)

    return [chunk for chunk in chunks if chunk.strip()]


def trim_body(body: str | None, *, max_chars: int = MAX_BODY_CHARS) -> str | None:
    """Trim body to max chars with ellipsis."""
    if not body:
        return None
    if len(body) > max_chars:
        body = body[: max_chars - 1] + "…"
    return body if body.strip() else None


def prepare_discord(parts: MarkdownParts) -> str:
    """Render markdown parts to Discord-compatible message."""
    trimmed = MarkdownParts(
        header=parts.header or "",
        body=trim_body(parts.body, max_chars=MAX_BODY_CHARS),
        footer=parts.footer,
    )
    # Discord supports markdown natively, so just assemble
    return assemble_markdown_parts(trimmed)


def prepare_discord_multi(
    parts: MarkdownParts, *, max_body_chars: int = MAX_BODY_CHARS
) -> list[str]:
    """Render markdown parts to multiple Discord messages if needed."""
    body = parts.body
    if body is not None and not body.strip():
        body = None
    body_chunks = split_markdown_body(body, max_body_chars) if body is not None else []
    if not body_chunks:
        body_chunks = [""]
    total = len(body_chunks)

    messages: list[str] = []
    for idx, chunk in enumerate(body_chunks, start=1):
        header = parts.header or ""
        if idx > 1:
            if header:
                header = f"{header} · continued ({idx}/{total})"
            else:
                header = f"continued ({idx}/{total})"
        messages.append(
            assemble_markdown_parts(
                MarkdownParts(header=header, body=chunk, footer=parts.footer)
            )
        )
    return messages
