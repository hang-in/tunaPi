"""Setup verification and interactive setup for Mattermost transport."""

from __future__ import annotations

from pathlib import Path

from ..backends import EngineBackend, SetupIssue
from ..config import HOME_CONFIG_PATH, load_or_init_config, write_config
from ..settings import load_settings_if_exists
from ..transports import SetupResult


def check_setup(
    engine_backend: EngineBackend,
    *,
    transport_override: str | None = None,
) -> SetupResult:
    """Check that the Mattermost transport is properly configured."""
    issues: list[SetupIssue] = []
    config_path = HOME_CONFIG_PATH

    result = load_settings_if_exists()
    if result is None:
        issues.append(
            SetupIssue(
                title="create a config",
                lines=(f"Missing config file {config_path}",),
            )
        )
        return SetupResult(issues=issues, config_path=config_path)

    settings, config_path = result
    transport = transport_override or settings.transport
    if transport != "mattermost":
        issues.append(
            SetupIssue(
                title="configure mattermost",
                lines=(
                    f"Transport is {settings.transport!r}, expected 'mattermost'.",
                    'Set transport = "mattermost" in config.',
                ),
            )
        )

    mm = settings.transports.mattermost
    if mm is None:
        issues.append(
            SetupIssue(
                title="configure mattermost",
                lines=(
                    "Missing [transports.mattermost] section.",
                    "Add url and token under [transports.mattermost].",
                ),
            )
        )
    else:
        if not mm.url:
            issues.append(
                SetupIssue(
                    title="mattermost url",
                    lines=("Missing transports.mattermost.url.",),
                )
            )
        if not mm.token:
            issues.append(
                SetupIssue(
                    title="mattermost token",
                    lines=(
                        "Missing transports.mattermost.token.",
                        "Set token in config or MATTERMOST_TOKEN in .env.",
                    ),
                )
            )

    # Check engine CLI
    if engine_backend.cli_cmd is not None:
        import shutil

        if shutil.which(engine_backend.cli_cmd) is None:
            issues.append(
                SetupIssue(
                    title=f"install {engine_backend.id}",
                    lines=(
                        f"Engine CLI `{engine_backend.cli_cmd}` not found on PATH.",
                        engine_backend.install_cmd or f"Install {engine_backend.id}.",
                    ),
                )
            )

    return SetupResult(issues=issues, config_path=config_path)


async def interactive_setup(*, force: bool) -> bool:
    """Minimal interactive setup for Mattermost transport."""
    import questionary

    config, config_path = load_or_init_config()

    if not force and config.get("transport") == "mattermost":
        mm = config.get("transports", {}).get("mattermost", {})
        if mm.get("url") and mm.get("token"):
            print("Mattermost transport is already configured.")
            proceed = await questionary.confirm(
                "Reconfigure?", default=False
            ).ask_async()
            if not proceed:
                return False

    url = await questionary.text(
        "Mattermost server URL (e.g. https://mm.company.com):"
    ).ask_async()
    if not url:
        return False

    token = await questionary.text(
        "Bot/Personal Access Token:"
    ).ask_async()
    if not token:
        return False

    config["transport"] = "mattermost"
    transports = config.setdefault("transports", {})
    mm = transports.setdefault("mattermost", {})
    mm["url"] = url.strip().rstrip("/")
    mm["token"] = token.strip()

    write_config(config, config_path)
    print(f"Config written to {config_path}")
    return True
