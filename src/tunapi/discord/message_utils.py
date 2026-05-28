from __future__ import annotations

import discord


def is_bot_mentioned(message: discord.Message, bot_user: discord.User | None) -> bool:
    """Check if the bot is mentioned in the message."""
    if bot_user is None:
        return False
    return bot_user in message.mentions


def should_process_message(
    message: discord.Message,
    bot_user: discord.User | None,
    *,
    require_mention: bool = False,
) -> bool:
    """Determine if a message should be processed by the bot."""
    if message.author.bot:
        return False

    if not message.content.strip() and not message.attachments:
        return False

    if isinstance(message.channel, discord.Thread):
        return True

    if require_mention:
        return is_bot_mentioned(message, bot_user)

    return True


def extract_prompt_from_message(
    message: discord.Message,
    bot_user: discord.User | None,
) -> str:
    """Extract the prompt text from a message, removing bot mentions."""
    content = message.content

    if bot_user is not None:
        content = content.replace(f"<@{bot_user.id}>", "").strip()
        content = content.replace(f"<@!{bot_user.id}>", "").strip()

    return content


def parse_branch_prefix(content: str) -> tuple[str | None, str]:
    """Parse @branch prefix from message content."""
    content = content.strip()
    if not content.startswith("@"):
        return None, content

    parts = content[1:].split(None, 1)
    if not parts:
        return None, content

    branch = parts[0]
    if not branch:
        return None, content

    remaining = parts[1] if len(parts) > 1 else ""
    return branch, remaining.strip()
