"""Setup and onboarding for Discord transport."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import anyio
import questionary
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from tunapi.backends import EngineBackend, SetupIssue
from tunapi.backends_helpers import install_issue
from tunapi.config import (
    ConfigError,
    HOME_CONFIG_PATH,
    dump_toml,
    ensure_table,
    read_config,
    write_config,
)
from tunapi.engines import list_backends
from tunapi.logging import suppress_logs
from tunapi.settings import load_settings
from tunapi.transports import SetupResult

__all__ = [
    "check_setup",
    "interactive_setup",
    "mask_token",
]


def _display_path(path: Path) -> str:
    home = Path.home()
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def _resolve_default_config_path() -> Path:
    cwd_path = Path.cwd() / "tunapi.toml"
    if cwd_path.exists():
        return cwd_path
    if HOME_CONFIG_PATH.exists():
        return HOME_CONFIG_PATH
    return cwd_path


_CREATE_CONFIG_TITLE = "create a config"
_CONFIGURE_DISCORD_TITLE = "configure discord"


def config_issue(
    path: Path,
    *,
    title: str,
    reason: str | None = None,
) -> SetupIssue:
    lines = [f"   {_display_path(path)}"]
    if reason:
        lines.append(f"   reason: {reason}")
    return SetupIssue(title, tuple(lines))


def _require_discord(settings, config_path: Path) -> Any:
    """Check if Discord transport is configured."""
    transports = getattr(settings, "transports", None)
    if transports is None:
        raise ConfigError(f"no transports configured in {config_path}")

    # Check for discord in extra fields since it's a plugin
    discord_config = getattr(transports, "discord", None)
    if discord_config is None:
        # Try model_extra for pydantic extra fields
        extra = getattr(transports, "model_extra", {}) or {}
        discord_config = extra.get("discord")

    if discord_config is None:
        raise ConfigError(f"discord transport not configured in {config_path}")

    # Validate required fields
    if isinstance(discord_config, dict):
        if not discord_config.get("bot_token"):
            raise ConfigError("discord.bot_token is required")
    else:
        if not getattr(discord_config, "bot_token", None):
            raise ConfigError("discord.bot_token is required")

    return discord_config


def check_setup(
    backend: EngineBackend,
    *,
    transport_override: str | None = None,
) -> SetupResult:
    """Check if Discord transport is properly set up."""
    issues: list[SetupIssue] = []
    config_path = _resolve_default_config_path()
    cmd = backend.cli_cmd or backend.id
    backend_issues: list[SetupIssue] = []
    if shutil.which(cmd) is None:
        backend_issues.append(install_issue(cmd, backend.install_cmd))

    try:
        settings, config_path = load_settings()
        if transport_override:
            settings = settings.model_copy(update={"transport": transport_override})
        try:
            _require_discord(settings, config_path)
        except ConfigError as exc:
            issues.append(
                config_issue(
                    config_path,
                    title=_CONFIGURE_DISCORD_TITLE,
                    reason=str(exc),
                )
            )
    except ConfigError as exc:
        issues.extend(backend_issues)
        title = (
            _CONFIGURE_DISCORD_TITLE
            if config_path.exists() and config_path.is_file()
            else _CREATE_CONFIG_TITLE
        )
        issues.append(config_issue(config_path, title=title, reason=str(exc)))
        return SetupResult(issues=issues, config_path=config_path)

    issues.extend(backend_issues)
    return SetupResult(issues=issues, config_path=config_path)


def mask_token(token: str) -> str:
    """Mask a bot token for display."""
    token = token.strip()
    if len(token) <= 12:
        return "*" * len(token)
    return f"{token[:9]}...{token[-5:]}"


async def _validate_discord_token(token: str) -> tuple[str, str] | None:
    """Validate a Discord bot token and return (bot_id, bot_name) if valid."""
    import discord

    intents = discord.Intents.default()
    # Use discord.Bot for Pycord
    client = discord.Bot(intents=intents)

    try:
        ready_event = anyio.Event()
        bot_info: dict[str, Any] = {}

        @client.event
        async def on_ready() -> None:
            if client.user:
                bot_info["id"] = str(client.user.id)
                bot_info["name"] = client.user.name
            ready_event.set()

        # Start client in background
        async def run_client() -> None:
            try:
                await client.start(token)
            except discord.LoginFailure:
                ready_event.set()
            except Exception:  # noqa: BLE001
                ready_event.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(run_client)

            # Wait for ready or timeout
            with anyio.move_on_after(10):
                await ready_event.wait()

            tg.cancel_scope.cancel()

        if bot_info.get("id"):
            return bot_info["id"], bot_info["name"]
        return None
    finally:
        if not client.is_closed():
            await client.close()


def _render_engine_table(console: Console) -> list[tuple[str, bool, str | None]]:
    """Render a table of available engines."""
    backends = list_backends()
    rows: list[tuple[str, bool, str | None]] = []
    table = Table(show_header=True, header_style="bold", box=box.SIMPLE)
    table.add_column("agent")
    table.add_column("status")
    table.add_column("install command")
    for backend in backends:
        cmd = backend.cli_cmd or backend.id
        installed = shutil.which(cmd) is not None
        status = "[green]✓ installed[/]" if installed else "[dim]✗ not found[/]"
        rows.append((backend.id, installed, backend.install_cmd))
        table.add_row(
            backend.id,
            status,
            "" if installed else (backend.install_cmd or "-"),
        )
    console.print(table)
    return rows


async def _confirm(message: str, *, default: bool = True) -> bool | None:
    """Simple yes/no confirmation."""
    result = await questionary.confirm(message, default=default).ask_async()
    return result


async def _prompt_token(console: Console) -> tuple[str, str, str] | None:
    """Prompt for and validate a Discord bot token.

    Returns (token, bot_id, bot_name) if successful.
    """
    while True:
        token = await questionary.password("paste your discord bot token:").ask_async()
        if token is None:
            return None
        token = token.strip()
        if not token:
            console.print("  token cannot be empty")
            continue
        console.print("  validating...")
        with suppress_logs():
            result = await _validate_discord_token(token)
        if result:
            bot_id, bot_name = result
            console.print(f"  connected to {bot_name} (ID: {bot_id})")
            return token, bot_id, bot_name
        console.print("  failed to connect, check the token and try again")
        retry = await _confirm("try again?", default=True)
        if not retry:
            return None


async def interactive_setup(*, force: bool) -> bool:
    """Run interactive setup for Discord transport."""
    console = Console()
    config_path = HOME_CONFIG_PATH

    if config_path.exists() and not force:
        console.print(
            f"config already exists at {_display_path(config_path)}. "
            "use --onboard to reconfigure."
        )
        return True

    if config_path.exists() and force:
        overwrite = await _confirm(
            f"update existing config at {_display_path(config_path)}?",
            default=False,
        )
        if not overwrite:
            return False

    with suppress_logs():
        panel = Panel(
            "let's set up your discord bot.",
            title="welcome to tunapi-discord!",
            border_style="blue",
            padding=(1, 2),
            expand=False,
        )
        console.print(panel)

        console.print("step 1: discord bot setup\n")
        have_token = await _confirm("do you have a discord bot token?")
        if have_token is None:
            return False
        if not have_token:
            console.print("  1. go to https://discord.com/developers/applications")
            console.print("  2. click 'New Application' and give it a name")
            console.print("  3. go to 'Bot' section and click 'Reset Token'")
            console.print("  4. copy the token")
            console.print(
                "  5. enable 'Message Content Intent' under Privileged Gateway Intents"
            )
            console.print("")

        token_info = await _prompt_token(console)
        if token_info is None:
            return False
        token, bot_id, bot_name = token_info

        console.print("\nstep 2: invite bot to server\n")
        # Generate invite URL with required permissions
        permissions = 277025770560  # Send messages, manage threads, etc.
        invite_url = f"https://discord.com/api/oauth2/authorize?client_id={bot_id}&permissions={permissions}&scope=bot%20applications.commands"
        console.print(f"  invite URL: {invite_url}")
        console.print("")
        console.print("  open the URL above and add the bot to your server")
        input("  press Enter when done...")

        # Optional: prompt for guild ID
        guild_id: int | None = None
        use_guild = await _confirm("restrict bot to a specific server?", default=False)
        if use_guild:
            guild_id_str = await questionary.text(
                "enter server (guild) ID:"
            ).ask_async()
            if guild_id_str:
                try:
                    guild_id = int(guild_id_str.strip())
                except ValueError:
                    console.print("  invalid guild ID, skipping")

        console.print("\nstep 3: agent cli tools")
        rows = _render_engine_table(console)
        installed_ids = [engine_id for engine_id, installed, _ in rows if installed]

        default_engine: str | None = None
        if installed_ids:
            default_engine = await questionary.select(
                "choose default agent:",
                choices=installed_ids,
            ).ask_async()
            if default_engine is None:
                return False
        else:
            console.print("no agents found on PATH. install one to continue.")
            save_anyway = await _confirm("save config anyway?", default=False)
            if not save_anyway:
                return False

        # Build preview config
        preview_config: dict[str, Any] = {}
        if default_engine is not None:
            preview_config["default_engine"] = default_engine
        preview_config["transport"] = "discord"
        discord_config: dict[str, Any] = {
            "bot_token": mask_token(token),
        }
        if guild_id is not None:
            discord_config["guild_id"] = guild_id
        preview_config["transports"] = {"discord": discord_config}

        config_preview = dump_toml(preview_config).rstrip()
        console.print("\nstep 4: save configuration\n")
        console.print(f"  {_display_path(config_path)}\n")
        for line in config_preview.splitlines():
            console.print(f"  {line}")
        console.print("")

        save = await _confirm(
            f"save this config to {_display_path(config_path)}?",
            default=True,
        )
        if not save:
            return False

        # Read existing config and merge
        raw_config: dict[str, Any] = {}
        if config_path.exists():
            try:
                raw_config = read_config(config_path)
            except ConfigError as exc:
                console.print(f"[yellow]warning:[/] config is malformed: {exc}")
                backup = config_path.with_suffix(".toml.bak")
                try:
                    shutil.copyfile(config_path, backup)
                except OSError as copy_exc:
                    console.print(
                        f"[yellow]warning:[/] failed to back up config: {copy_exc}"
                    )
                else:
                    console.print(f"  backed up to {_display_path(backup)}")
                raw_config = {}

        merged = dict(raw_config)
        if default_engine is not None:
            merged["default_engine"] = default_engine
        merged["transport"] = "discord"
        transports = ensure_table(merged, "transports", config_path=config_path)
        discord_section = ensure_table(
            transports,
            "discord",
            config_path=config_path,
            label="transports.discord",
        )
        discord_section["bot_token"] = token
        if guild_id is not None:
            discord_section["guild_id"] = guild_id

        write_config(merged, config_path)
        console.print(f"  config saved to {_display_path(config_path)}")

        done_panel = Panel(
            "setup complete. run 'tunapi' (or 'tunapi --transport discord') "
            "to start tunapi-discord!\n\n"
            "tip: the first channel you message the bot in will become\n"
            "the startup channel where status messages are posted.",
            border_style="green",
            padding=(1, 2),
            expand=False,
        )
        console.print("\n")
        console.print(done_panel)
        return True
