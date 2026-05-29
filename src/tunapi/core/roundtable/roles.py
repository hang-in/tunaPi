"""Role-typed behavioral directives for roundtable agents.

Pure module (no I/O). Adapted from tunaFlow's `roundtable_helpers` role
guidance, generalized for open discussion (not code review). Roles let the
same engine play distinct parts (proposer / reviewer / verifier /
synthesizer) with their own directive and output-length budget.

Generated via tunaLlama (glm-5.1:cloud) and reviewed; `_GUIDANCE` hoisted to
module level.
"""

from __future__ import annotations

# ── Alias → canonical role mapping ──────────────────────────────────────────
_ROLE_ALIASES: dict[str, str] = {
    "proposer": "proposer",
    "reviewer": "reviewer",
    "critic": "reviewer",
    "verifier": "verifier",
    "judge": "verifier",
    "synthesizer": "synthesizer",
    "lead": "synthesizer",
}

# ── Token caps per canonical role ───────────────────────────────────────────
_ROLE_TOKEN_CAPS: dict[str, int] = {
    "proposer": 1200,
    "reviewer": 900,
    "verifier": 800,
    "synthesizer": 2000,
}

# Module-level constants (derived from the dict to avoid drift).
PROPOSER_MAX_TOKENS: int = _ROLE_TOKEN_CAPS["proposer"]
REVIEWER_MAX_TOKENS: int = _ROLE_TOKEN_CAPS["reviewer"]
VERIFIER_MAX_TOKENS: int = _ROLE_TOKEN_CAPS["verifier"]
SYNTHESIZER_MAX_TOKENS: int = _ROLE_TOKEN_CAPS["synthesizer"]

# ── Per-role directives (open discussion, domain-neutral) ───────────────────
_GUIDANCE: dict[str, str] = {
    "proposer": "\n".join([
        "Put forward a clear position or proposal with concrete rationale.",
        "State your key claims up front; support each with evidence or examples.",
        "Keep the proposal focused and actionable.",
        "Invite specific critique rather than seeking blanket agreement.",
    ]),
    "reviewer": "\n".join([
        "Critique others' proposals: identify strengths, weaknesses, and risks.",
        "Be specific — reference exact claims rather than vague impressions.",
        "Acknowledge what works before flagging concerns.",
        "End with a one-line verdict: agree / disagree / conditional.",
    ]),
    "verifier": "\n".join([
        "Independently judge the soundness of each proposal.",
        "Do NOT defer to other participants; verify claims from first principles.",
        "Flag any unsupported or contradictory claims explicitly.",
        "State your own conclusion clearly, even if it diverges from the group.",
    ]),
    "synthesizer": "\n".join([
        "Reduce all responses into: ## Consensus, ## Disagreements, ## Open questions.",
        "Preserve each participant's verdict — do not overwrite or reinterpret them.",
        "Highlight where proposals align and where they conflict.",
        "End with a final recommendation grounded in the discussion.",
    ]),
}


def canonical_role(role: str | None) -> str | None:
    """Normalize a role string to its canonical name, or ``None`` if unknown."""
    if not role:
        return None
    return _ROLE_ALIASES.get(role.strip().lower())


def role_guidance(role: str | None) -> str:
    """Concise behavioral directive for *role*; ``""`` for unknown/None."""
    canonical = canonical_role(role)
    if canonical is None:
        return ""
    return _GUIDANCE.get(canonical, "")


def effective_max_tokens(role: str | None, override: int | None = None) -> int | None:
    """Effective output-token cap for *role* (``override`` wins; ``None`` if unknown)."""
    if override is not None:
        return override
    canonical = canonical_role(role)
    if canonical is None:
        return None
    return _ROLE_TOKEN_CAPS.get(canonical)
