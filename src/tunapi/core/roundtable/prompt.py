"""Roundtable round-prompt builder."""

from __future__ import annotations

# Maximum length of an agent answer included in context prompts.
_MAX_ANSWER_LENGTH = 4000


def _build_round_prompt(
    topic: str,
    transcript: list[tuple[str, str]],
    round_num: int,
    current_round_responses: list[tuple[str, str]] | None = None,
) -> str:
    """Build the prompt for a given round.

    Includes previous rounds' transcript and any same-round responses
    that have been collected so far.
    """
    sections: list[str] = []

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

    if not sections:
        return topic

    context_block = "\n\n---\n\n".join(sections)
    return f"{context_block}\n\n---\n\n위 의견들을 참고하여 답변해주세요: {topic}"
