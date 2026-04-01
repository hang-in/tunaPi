"""Type definitions for Discord transport."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DiscordIncomingMessage:
    """Incoming message from Discord."""

    transport: str
    guild_id: int | None
    channel_id: int
    message_id: int
    content: str
    author_id: int
    author_name: str
    thread_id: int | None = None
    reply_to_message_id: int | None = None
    reply_to_content: str | None = None
    category_id: int | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DiscordInteraction:
    """Interaction from Discord (slash commands, buttons)."""

    transport: str
    guild_id: int | None
    channel_id: int
    interaction_id: int
    interaction_token: str
    command_name: str | None
    custom_id: str | None
    user_id: int
    user_name: str
    options: dict[str, Any] | None = None
    message_id: int | None = None
    raw: Any | None = None


@dataclass(frozen=True, slots=True)
class DiscordChannelContext:
    """Context for a Discord channel bound to a project.

    Channels are bound to a project with configuration for worktrees.
    The worktree_base is the default branch used when no @branch is specified.
    """

    project: str
    worktrees_dir: str = ".worktrees"
    default_engine: str = "claude"
    worktree_base: str = "main"


@dataclass(frozen=True, slots=True)
class DiscordThreadContext:
    """Context for a Discord thread bound to a specific branch.

    Threads are created via @branch prefix and work on a specific branch
    (as a worktree from the channel's worktree_base).
    """

    project: str
    branch: str
    worktrees_dir: str = ".worktrees"
    default_engine: str = "claude"


@dataclass(frozen=True, slots=True)
class DiscordChannelState:
    """State for a Discord channel or thread."""

    # For channels: DiscordChannelContext (project config, no branch)
    # For threads: DiscordThreadContext (project + specific branch)
    context: DiscordChannelContext | DiscordThreadContext | None = None
    sessions: dict[str, str] | None = None  # engine_id -> resume_token
