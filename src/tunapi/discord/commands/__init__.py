"""Command plugin system integration for Discord."""

from .dispatch import dispatch_command, split_command_args
from .executor import _DiscordCommandExecutor
from .registration import discover_command_ids, register_plugin_commands

__all__ = [
    "dispatch_command",
    "discover_command_ids",
    "register_plugin_commands",
    "split_command_args",
    "_DiscordCommandExecutor",
]
