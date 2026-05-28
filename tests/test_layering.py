from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "src" / "tunapi" / "core"
FORBIDDEN_TRANSPORTS = {
    "tunapi.discord",
    "tunapi.mattermost",
    "tunapi.slack",
    "tunapi.telegram",
    "tunapi.tunadish",
}


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_core_does_not_import_transport_modules() -> None:
    offenders: list[str] = []
    for path in sorted(CORE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            if any(
                module == prefix or module.startswith(f"{prefix}.")
                for prefix in FORBIDDEN_TRANSPORTS
            ):
                rel = path.relative_to(ROOT)
                offenders.append(f"{rel}: {module}")

    assert offenders == []
