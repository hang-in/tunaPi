from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src" / "tunapi"
MAX_RECOMMENDED_LINES = 800

# Current baseline for the fat-file decomposition plan. Refactors may remove
# entries from this set, but new oversized files should not be introduced.
ALLOWED_OVERSIZED_FILES = {
    "src/tunapi/discord/handlers.py",
    "src/tunapi/mattermost/commands.py",
    "src/tunapi/mattermost/loop.py",
    "src/tunapi/runner_bridge.py",
    "src/tunapi/slack/commands.py",
    "src/tunapi/slack/loop.py",
    "src/tunapi/telegram/loop_dispatch.py",
    "src/tunapi/telegram/onboarding.py",
    "src/tunapi/tunadish/commands.py",
}


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_no_new_python_files_exceed_size_baseline() -> None:
    oversized = {
        str(path.relative_to(ROOT))
        for path in SRC_DIR.rglob("*.py")
        if _line_count(path) > MAX_RECOMMENDED_LINES
    }

    assert oversized <= ALLOWED_OVERSIZED_FILES
