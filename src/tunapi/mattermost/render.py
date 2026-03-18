"""Render progress and final messages as Mattermost Markdown.

Mattermost supports Markdown natively, so this is much simpler than the
Telegram renderer which must convert to entities arrays.
"""

from __future__ import annotations

from ..markdown import (
    MarkdownFormatter,
    MarkdownParts,
    assemble_markdown_parts,
)

MAX_POST_LENGTH = 16383  # Mattermost post size limit


def trim_body(body: str | None, max_chars: int = MAX_POST_LENGTH) -> str | None:
    if not body or not body.strip():
        return None
    if len(body) <= max_chars:
        return body
    return body[: max_chars - 1] + "…"


def split_markdown_body(body: str, max_chars: int) -> list[str]:
    """Split a markdown body into chunks that fit within *max_chars*.

    Splits at paragraph boundaries (blank lines) and respects code fences.
    """
    if len(body) <= max_chars:
        return [body]

    paragraphs = body.split("\n\n")
    chunks: list[str] = []
    current = ""
    in_fence = False

    for para in paragraphs:
        fence_count = para.count("```")
        candidate = f"{current}\n\n{para}" if current else para

        if len(candidate) > max_chars and current:
            if in_fence:
                current += "\n```"
            chunks.append(current.strip())
            current = f"```\n{para}" if in_fence else para
        else:
            current = candidate

        if fence_count % 2 == 1:
            in_fence = not in_fence

    if current.strip():
        chunks.append(current.strip())

    return chunks or [body[:max_chars]]


def prepare_mattermost(parts: MarkdownParts) -> str:
    """Assemble markdown parts into a single Mattermost message string."""
    body = trim_body(parts.body)
    trimmed = MarkdownParts(header=parts.header, body=body, footer=parts.footer)
    return assemble_markdown_parts(trimmed)


def prepare_mattermost_multi(
    parts: MarkdownParts,
    max_body_chars: int = MAX_POST_LENGTH,
) -> list[str]:
    """Split a message into multiple posts if the body is too long."""
    body = parts.body or ""
    if not body.strip():
        return [assemble_markdown_parts(parts)]

    chunks = split_markdown_body(body, max_body_chars)
    if len(chunks) == 1:
        return [assemble_markdown_parts(parts)]

    messages: list[str] = []
    total = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        header = parts.header if idx == 1 else f"continued ({idx}/{total})"
        footer = parts.footer if idx == total else None
        messages.append(
            assemble_markdown_parts(MarkdownParts(header=header, body=chunk, footer=footer))
        )
    return messages
