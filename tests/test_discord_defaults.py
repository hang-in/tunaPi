"""Tests for default values and fallbacks."""

from __future__ import annotations

import json

import pytest

from tunapi.discord.overrides import resolve_trigger_mode
from tunapi.discord.prefs import DiscordPrefsStore
from tunapi.discord.state import DiscordStateStore
from tunapi.discord.types import DiscordChannelContext


def test_channel_context_default_base_branch_is_main() -> None:
    ctx = DiscordChannelContext(project="~/dev/example")
    assert ctx.worktree_base == "main"


@pytest.mark.anyio
async def test_state_store_defaults_worktree_base_to_main_when_missing(
    tmp_path,
) -> None:
    config_path = tmp_path / "tunapi.toml"
    state_path = tmp_path / "discord_state.json"

    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "channels": {
                    "123:456": {
                        "context": {
                            "project": "~/dev/example",
                        }
                    }
                },
                "guilds": {},
            }
        ),
        encoding="utf-8",
    )

    store = DiscordStateStore(config_path=config_path)
    context = await store.get_context(123, 456)

    assert context is not None
    assert isinstance(context, DiscordChannelContext)
    assert context.worktree_base == "main"


@pytest.mark.anyio
async def test_trigger_mode_uses_config_default_when_no_overrides(tmp_path) -> None:
    config_path = tmp_path / "tunapi.toml"
    prefs = DiscordPrefsStore(config_path)

    resolved = await resolve_trigger_mode(
        prefs,
        guild_id=123,
        channel_id=456,
        thread_id=None,
        default_mode="mentions",
    )

    assert resolved == "mentions"


@pytest.mark.anyio
async def test_trigger_mode_override_precedence_over_config_default(tmp_path) -> None:
    config_path = tmp_path / "tunapi.toml"
    prefs = DiscordPrefsStore(config_path)

    # Channel override should beat config default.
    await prefs.set_trigger_mode(123, 456, "all")
    resolved_channel = await resolve_trigger_mode(
        prefs,
        guild_id=123,
        channel_id=456,
        thread_id=None,
        default_mode="mentions",
    )
    assert resolved_channel == "all"

    # Thread override should beat channel override and config default.
    await prefs.set_trigger_mode(123, 789, "mentions")
    resolved_thread = await resolve_trigger_mode(
        prefs,
        guild_id=123,
        channel_id=456,
        thread_id=789,
        default_mode="all",
    )
    assert resolved_thread == "mentions"
