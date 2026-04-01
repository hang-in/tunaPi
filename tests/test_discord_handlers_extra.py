"""Extra tests for discord handlers – covers functions not in test_discord_handlers.py."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import discord
import pytest

from tunapi.discord.handlers import (
    _is_admin,
    _normalize_branch_name,
    extract_prompt_from_message,
    is_bot_mentioned,
    should_process_message,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(*, guild: object | None = MagicMock(), author: object | None = None) -> MagicMock:
    """Build a minimal mock ApplicationContext."""
    ctx = MagicMock(spec=discord.ApplicationContext)
    ctx.guild = guild
    if author is not None:
        ctx.author = author
    return ctx


def _make_member(*, administrator: bool = False) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    perms = MagicMock(spec=discord.Permissions)
    perms.administrator = administrator
    type(member).guild_permissions = PropertyMock(return_value=perms)
    return member


def _make_message(
    *,
    content: str = "",
    bot: bool = False,
    mentions: list | None = None,
    channel: object | None = None,
    attachments: list | None = None,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.content = content
    msg.author = MagicMock()
    msg.author.bot = bot
    msg.mentions = mentions or []
    msg.channel = channel or MagicMock(spec=discord.TextChannel)
    msg.attachments = attachments or []
    return msg


def _make_bot_user(user_id: int = 12345) -> MagicMock:
    user = MagicMock(spec=discord.User)
    user.id = user_id
    return user


# ===========================================================================
# _is_admin
# ===========================================================================

class TestIsAdmin:
    def test_no_guild_returns_false(self) -> None:
        ctx = _make_ctx(guild=None)
        assert _is_admin(ctx) is False

    def test_author_is_admin_member(self) -> None:
        member = _make_member(administrator=True)
        ctx = _make_ctx(author=member)
        assert _is_admin(ctx) is True

    def test_author_is_non_admin_member(self) -> None:
        member = _make_member(administrator=False)
        ctx = _make_ctx(author=member)
        assert _is_admin(ctx) is False

    def test_author_is_plain_user_not_member(self) -> None:
        """A discord.User (not Member) should return False."""
        user = MagicMock(spec=discord.User)
        ctx = _make_ctx(author=user)
        assert _is_admin(ctx) is False


# ===========================================================================
# _normalize_branch_name
# ===========================================================================

class TestNormalizeBranchName:
    def test_plain_name(self) -> None:
        assert _normalize_branch_name("main") == "main"

    def test_leading_at_stripped(self) -> None:
        assert _normalize_branch_name("@main") == "main"

    def test_whitespace_stripped(self) -> None:
        assert _normalize_branch_name("  main  ") == "main"

    def test_at_with_whitespace(self) -> None:
        assert _normalize_branch_name("  @feature/foo  ") == "feature/foo"

    def test_empty_string(self) -> None:
        assert _normalize_branch_name("") == ""

    def test_just_at(self) -> None:
        assert _normalize_branch_name("@") == ""

    def test_double_at(self) -> None:
        # Only the first @ is stripped
        assert _normalize_branch_name("@@branch") == "@branch"

    def test_at_in_middle_not_stripped(self) -> None:
        assert _normalize_branch_name("feat@thing") == "feat@thing"

    def test_slash_branch(self) -> None:
        assert _normalize_branch_name("@chore/cleanup") == "chore/cleanup"

    def test_hyphen_branch(self) -> None:
        assert _normalize_branch_name("@fix-123") == "fix-123"


# ===========================================================================
# is_bot_mentioned
# ===========================================================================

class TestIsBotMentioned:
    def test_bot_user_none(self) -> None:
        msg = _make_message(content="hello")
        assert is_bot_mentioned(msg, None) is False

    def test_bot_in_mentions(self) -> None:
        bot = _make_bot_user()
        msg = _make_message(content="hello", mentions=[bot])
        assert is_bot_mentioned(msg, bot) is True

    def test_bot_not_in_mentions(self) -> None:
        bot = _make_bot_user()
        other = _make_bot_user(user_id=99999)
        msg = _make_message(content="hello", mentions=[other])
        assert is_bot_mentioned(msg, bot) is False

    def test_empty_mentions(self) -> None:
        bot = _make_bot_user()
        msg = _make_message(content="hello", mentions=[])
        assert is_bot_mentioned(msg, bot) is False


# ===========================================================================
# should_process_message
# ===========================================================================

class TestShouldProcessMessage:
    def test_bot_author_ignored(self) -> None:
        msg = _make_message(content="hi", bot=True)
        assert should_process_message(msg, None) is False

    def test_empty_content_no_attachments_ignored(self) -> None:
        msg = _make_message(content="   ", bot=False)
        assert should_process_message(msg, None) is False

    def test_empty_content_with_attachments_processed(self) -> None:
        attachment = MagicMock()
        msg = _make_message(content="", bot=False, attachments=[attachment])
        assert should_process_message(msg, None) is True

    def test_normal_message_in_channel(self) -> None:
        msg = _make_message(content="hello", bot=False)
        assert should_process_message(msg, None) is True

    def test_thread_message_always_processed(self) -> None:
        thread_channel = MagicMock(spec=discord.Thread)
        msg = _make_message(content="hello", bot=False, channel=thread_channel)
        assert should_process_message(msg, None, require_mention=True) is True

    def test_require_mention_true_no_mention(self) -> None:
        bot = _make_bot_user()
        msg = _make_message(content="hello", bot=False, mentions=[])
        assert should_process_message(msg, bot, require_mention=True) is False

    def test_require_mention_true_with_mention(self) -> None:
        bot = _make_bot_user()
        msg = _make_message(content="hello", bot=False, mentions=[bot])
        assert should_process_message(msg, bot, require_mention=True) is True

    def test_require_mention_false_no_mention(self) -> None:
        bot = _make_bot_user()
        msg = _make_message(content="hello", bot=False, mentions=[])
        assert should_process_message(msg, bot, require_mention=False) is True

    def test_bot_message_in_thread_still_ignored(self) -> None:
        thread_channel = MagicMock(spec=discord.Thread)
        msg = _make_message(content="hello", bot=True, channel=thread_channel)
        assert should_process_message(msg, None) is False


# ===========================================================================
# extract_prompt_from_message
# ===========================================================================

class TestExtractPromptFromMessage:
    def test_no_bot_user(self) -> None:
        msg = _make_message(content="hello world")
        assert extract_prompt_from_message(msg, None) == "hello world"

    def test_removes_standard_mention(self) -> None:
        bot = _make_bot_user(user_id=42)
        msg = _make_message(content="<@42> do something")
        assert extract_prompt_from_message(msg, bot) == "do something"

    def test_removes_nickname_mention(self) -> None:
        bot = _make_bot_user(user_id=42)
        msg = _make_message(content="<@!42> do something")
        assert extract_prompt_from_message(msg, bot) == "do something"

    def test_removes_both_mention_styles(self) -> None:
        bot = _make_bot_user(user_id=42)
        msg = _make_message(content="<@42> hey <@!42> there")
        assert extract_prompt_from_message(msg, bot) == "hey  there"

    def test_no_mention_content_unchanged(self) -> None:
        bot = _make_bot_user(user_id=42)
        msg = _make_message(content="just a message")
        assert extract_prompt_from_message(msg, bot) == "just a message"

    def test_only_mention(self) -> None:
        bot = _make_bot_user(user_id=42)
        msg = _make_message(content="<@42>")
        assert extract_prompt_from_message(msg, bot) == ""

    def test_mention_with_surrounding_spaces(self) -> None:
        bot = _make_bot_user(user_id=42)
        msg = _make_message(content="  <@42>  hello  ")
        assert extract_prompt_from_message(msg, bot) == "hello"

    def test_different_user_mention_preserved(self) -> None:
        bot = _make_bot_user(user_id=42)
        msg = _make_message(content="<@99> do something")
        assert extract_prompt_from_message(msg, bot) == "<@99> do something"

    def test_empty_content(self) -> None:
        bot = _make_bot_user(user_id=42)
        msg = _make_message(content="")
        assert extract_prompt_from_message(msg, bot) == ""
