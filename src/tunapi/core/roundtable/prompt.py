"""Roundtable round-prompt builder."""

from __future__ import annotations

from .roles import role_guidance

# Maximum length of an agent answer included in context prompts.
_MAX_ANSWER_LENGTH = 4000


def _build_round_prompt(
    topic: str,
    transcript: list[tuple[str, str]],
    round_num: int,
    current_round_responses: list[tuple[str, str]] | None = None,
    role: str | None = None,
    consensus: list[str] | None = None,
) -> str:
    """Build the prompt for a given round.

    Includes previous rounds' transcript and any same-round responses
    that have been collected so far. When *role* is set, the role directive is
    prepended; when *consensus* is set, accumulated agreements are injected as
    "do not re-litigate". role=None/consensus=None reproduces the original
    prompt exactly.
    """
    sections: list[str] = []

    # Consensus reached so far (don't re-litigate)
    if consensus:
        agreed = "\n".join(f"- {item}" for item in consensus)
        sections.append(
            "이미 합의된 사항 (다시 논쟁하지 말고 전제로 삼으세요):\n\n" + agreed
        )

    # Previous rounds context
    if transcript:
        context_lines: list[str] = []
        for engine, answer in transcript:
            trimmed = (
                answer[:_MAX_ANSWER_LENGTH] + "..."
                if len(answer) > _MAX_ANSWER_LENGTH
                else answer
            )
            context_lines.append(f"**[{engine}]**:\n{trimmed}")
        sections.append("이전 라운드 응답:\n\n" + "\n\n".join(context_lines))

    # Same-round earlier responses
    if current_round_responses:
        current_lines: list[str] = []
        for engine, answer in current_round_responses:
            trimmed = (
                answer[:_MAX_ANSWER_LENGTH] + "..."
                if len(answer) > _MAX_ANSWER_LENGTH
                else answer
            )
            current_lines.append(f"**[{engine}]**:\n{trimmed}")
        sections.append(
            "이번 라운드 다른 에이전트 답변:\n\n" + "\n\n".join(current_lines)
        )

    directive = role_guidance(role) if role else ""

    if not sections:
        body = topic
    else:
        context_block = "\n\n---\n\n".join(sections)
        body = f"{context_block}\n\n---\n\n위 의견들을 참고하여 답변해주세요: {topic}"

    if directive:
        return f"## Your role\n{directive}\n\n---\n\n{body}"
    return body
