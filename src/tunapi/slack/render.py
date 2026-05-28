"""Render progress and final messages as Slack mrkdwn."""

from __future__ import annotations

import re

from ..markdown import (
    MarkdownParts,
    assemble_markdown_parts,
)

MAX_POST_LENGTH = 3800  # Slack recommended limit to avoid truncation


def escape_slack(text: str) -> str:
    """Escape Slack special characters: &, <, >."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def markdown_to_mrkdwn(text: str) -> str:
    """Convert a subset of Markdown to Slack mrkdwn.

    Currently handles:
    - Escaping special characters
    - Converting [text](url) to <url|text>
    - Simple bold/italic conversion (though Tunapi rarely uses them)
    """
    if not text:
        return text

    # 1. Escape basic special characters
    text = escape_slack(text)

    # 2. Convert links: [text](url) -> <url|text>
    # Note: Regex must be careful not to match across line breaks or other constructs
    text = re.sub(r"\[([^\]\n]+)\]\(([^)\n]+)\)", r"<\2|\1>", text)

    # 3. Bold: **text** -> *text*
    text = re.sub(r"\*\*([^*]+)\*\*", r"*\1*", text)

    # 4. Italic: *text* -> _text_ (only if not inside a word or part of bold)
    # This is trickier to get perfect without a full parser, but let's do a basic one.
    # Tunapi's MarkdownFormatter doesn't currently use italics.

    return text


def trim_body(body: str | None, max_chars: int = MAX_POST_LENGTH) -> str | None:
    if not body or not body.strip():
        return None
    if len(body) <= max_chars:
        return body
    return body[: max_chars - 1] + "…"


def split_mrkdwn_body(body: str, max_chars: int) -> list[str]:
    """Split a mrkdwn body into chunks that fit within *max_chars*.

    Similar to Mattermost split but with Slack-specific considerations.
    """
    if len(body) <= max_chars:
        return [body]

    # Try to split at paragraph boundaries
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


def prepare_slack(parts: MarkdownParts) -> str:
    """Assemble markdown parts and convert to Slack mrkdwn."""
    header = markdown_to_mrkdwn(parts.header)
    body = markdown_to_mrkdwn(trim_body(parts.body) or "")
    footer = markdown_to_mrkdwn(parts.footer or "")

    return assemble_markdown_parts(
        MarkdownParts(header=header, body=body, footer=footer)
    )


def prepare_slack_multi(
    parts: MarkdownParts,
    max_body_chars: int = MAX_POST_LENGTH,
) -> list[str]:
    """Split a message into multiple posts if the body is too long."""
    body = parts.body or ""
    if not body.strip():
        return [prepare_slack(parts)]

    # We convert to mrkdwn AFTER splitting to avoid splitting inside escaped sequences or links
    # But wait, splitting depends on length, and length changes after conversion.
    # To be safe, we convert first, then split.

    mrkdwn_body = markdown_to_mrkdwn(body)
    header = markdown_to_mrkdwn(parts.header)
    footer = markdown_to_mrkdwn(parts.footer or "")

    chunks = split_mrkdwn_body(mrkdwn_body, max_body_chars)
    if len(chunks) == 1:
        return [
            assemble_markdown_parts(
                MarkdownParts(header=header, body=chunks[0], footer=footer)
            )
        ]

    messages: list[str] = []
    total = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        h = header if idx == 1 else f"*continued ({idx}/{total})*"
        f = footer if idx == total else None
        messages.append(
            assemble_markdown_parts(MarkdownParts(header=h, body=chunk, footer=f))
        )
    return messages
