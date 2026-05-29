"""Parse a roundtable synthesizer agent's free-text output into a structured synthesis.

Pure module (no I/O). Marker-first (`<!-- consensus {json} -->`), falling back to
markdown sections (## Consensus / ## Disagreements / ## Open questions /
recommendation). Returns ``None`` when no structure is found so callers can fall
back to the plain summary.

Generated via tunaLlama (glm-5.1:cloud) and reviewed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ExtractedSynthesis:
    """Structured result of parsing a synthesizer agent's output."""

    thesis: str = ""
    agreements: list[str] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)


def extract_synthesis(text: str) -> ExtractedSynthesis | None:
    """Extract structured synthesis from synthesizer agent output text."""
    if not text or not text.strip():
        return None

    # Path a: marker (highest priority)
    marker_result = _try_parse_marker(text)
    if marker_result is not None:
        if _is_non_empty(marker_result):
            return marker_result
        return None

    # Path b: markdown sections fallback
    md_result = _parse_markdown_sections(text)
    if md_result is None or not _is_non_empty(md_result):
        return None
    return md_result


def _is_non_empty(synth: ExtractedSynthesis) -> bool:
    return bool(
        synth.thesis or synth.agreements or synth.disagreements or synth.open_questions
    )


def _try_parse_marker(text: str) -> ExtractedSynthesis | None:
    """Parse an ``<!-- consensus {json} -->`` marker, or None if absent/invalid."""
    match = re.search(r"<!--\s*consensus\s+(.*?)\s*-->", text, re.DOTALL)
    if not match:
        return None

    try:
        obj: Any = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None

    thesis = ""
    for key in ("recommendation", "thesis"):
        val = obj.get(key)
        if isinstance(val, str):
            thesis = val
            break

    agreements: list[str] = []
    disagreements: list[str] = []
    open_questions: list[str] = []
    for key, target in (
        ("agreements", agreements),
        ("disagreements", disagreements),
        ("open_questions", open_questions),
    ):
        val = obj.get(key)
        if isinstance(val, list):
            target.extend(item for item in val if isinstance(item, str))

    return ExtractedSynthesis(
        thesis=thesis,
        agreements=agreements,
        disagreements=disagreements,
        open_questions=open_questions,
    )


def _classify_title(title: str) -> str | None:
    """Classify a markdown section title into a synthesis category."""
    lower = title.lower().strip()
    if any(kw in lower for kw in ("recommendation", "conclusion", "final")):
        return "thesis"
    # 'disagreement' before 'agreement' (the former contains the latter).
    if any(kw in lower for kw in ("disagreement", "contested")):
        return "disagreements"
    if any(kw in lower for kw in ("open question", "unresolved")):
        return "open_questions"
    if any(kw in lower for kw in ("consensus", "agreement")):
        return "agreements"
    return None


_BULLET_RE = re.compile(r"^[-*]\s+(.+)$")
_NUMBERED_BULLET_RE = re.compile(r"^\d+[.)]\s+(.+)$")
_HEADER_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)


def _extract_bullets(text: str) -> list[str]:
    bullets: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = _BULLET_RE.match(stripped) or _NUMBERED_BULLET_RE.match(stripped)
        if m:
            bullets.append(m.group(1).strip())
    return bullets


def _extract_thesis_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " ".join(lines)


def _parse_markdown_sections(text: str) -> ExtractedSynthesis | None:
    headers = list(_HEADER_RE.finditer(text))
    if not headers:
        return None

    thesis = ""
    agreements: list[str] = []
    disagreements: list[str] = []
    open_questions: list[str] = []
    found_section = False

    for i, hdr in enumerate(headers):
        start = hdr.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        section_text = text[start:end]

        category = _classify_title(hdr.group(2))
        if category is None:
            continue
        found_section = True

        if category == "thesis":
            thesis = _extract_thesis_text(section_text)
        elif category == "agreements":
            agreements.extend(_extract_bullets(section_text))
        elif category == "disagreements":
            disagreements.extend(_extract_bullets(section_text))
        elif category == "open_questions":
            open_questions.extend(_extract_bullets(section_text))

    if not found_section:
        return None

    return ExtractedSynthesis(
        thesis=thesis,
        agreements=agreements,
        disagreements=disagreements,
        open_questions=open_questions,
    )
