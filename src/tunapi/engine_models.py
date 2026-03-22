"""Engine-specific model registry and discovery.

Discovers available models dynamically from each engine's local sources:
- **Codex**: reads ``~/.codex/models_cache.json`` (auto-cached by CLI)
- **Gemini**: reads constants from installed ``@google/gemini-cli-core`` npm package
- **Claude**: falls back to static list (OAuth-only, no local model cache)

Results are cached in-process with a configurable TTL.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from .logging import get_logger

logger = get_logger(__name__)

# -- Fallback registry (used when dynamic discovery fails) ------------------

_FALLBACK_MODELS: dict[str, list[str]] = {
    "claude": [
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ],
    "codex": [],
    "gemini": [],
    "opencode": [],
    "pi": [],
}

# -- Discovery functions per engine ----------------------------------------

_CACHE_TTL_S = 3600  # 1 hour
_cache: dict[str, tuple[list[str], str, float]] = {}


def _discover_codex() -> list[str] | None:
    """Read Codex models from ``~/.codex/models_cache.json``."""
    cache_path = Path.home() / ".codex" / "models_cache.json"
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        models = []
        for m in data.get("models", []):
            slug = m.get("slug", "")
            vis = m.get("visibility", "")
            if slug and vis != "hide":
                models.append(slug)
        return models if models else None
    except Exception as exc:
        logger.debug("codex.model_discovery_failed", error=str(exc))
        return None


def _discover_gemini() -> list[str] | None:
    """Read Gemini model constants from installed ``@google/gemini-cli-core``."""
    script = """\
try {
    const path = require('path');
    const appdata = process.env.APPDATA || path.join(require('os').homedir(), '.npm-global');
    const corePath = path.join(appdata, 'npm/node_modules/@google/gemini-cli/node_modules/@google/gemini-cli-core');
    const core = require(corePath);
    const models = [];
    const keys = Object.keys(core).filter(k => k.includes('GEMINI') && k.includes('MODEL') && !k.includes('ALIAS') && !k.includes('EMBEDDING') && !k.includes('AUTO'));
    keys.forEach(k => {
        const v = core[k];
        if (typeof v === 'string' && v.startsWith('gemini-') && !v.includes('lite') && !v.includes('customtools')) models.push(v);
    });
    console.log(JSON.stringify(models));
} catch(e) {
    console.log('[]');
}
"""
    try:
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            models = json.loads(result.stdout.strip())
            return models if models else None
    except Exception as exc:
        logger.debug("gemini.model_discovery_failed", error=str(exc))
    return None


def _discover_claude() -> list[str] | None:
    """Claude uses OAuth — no local model cache available.

    Returns None to fall back to the static list.
    """
    return None


_DISCOVERERS: dict[str, object] = {
    "claude": _discover_claude,
    "codex": _discover_codex,
    "gemini": _discover_gemini,
}


# -- Public API -------------------------------------------------------------


def get_models(engine: str) -> tuple[list[str], str]:
    """Return ``(model_list, source)`` for the given engine.

    Tries dynamic discovery first, falls back to ``_FALLBACK_MODELS``.
    Results are cached for ``_CACHE_TTL_S`` seconds.

    *source* is ``"discovered"`` or ``"fallback"`` or ``"unknown"``.
    """
    now = time.monotonic()
    cached = _cache.get(engine)
    if cached is not None:
        models, source, ts = cached
        if now - ts < _CACHE_TTL_S:
            return list(models), source

    discoverer = _DISCOVERERS.get(engine)
    if discoverer is not None:
        try:
            discovered = discoverer()
            if discovered:
                _cache[engine] = (discovered, "discovered", now)
                return list(discovered), "discovered"
        except Exception as exc:
            logger.debug("model_discovery_error", engine=engine, error=str(exc))

    fallback = _FALLBACK_MODELS.get(engine)
    if fallback:
        _cache[engine] = (fallback, "fallback", now)
        return list(fallback), "fallback"

    return [], "unknown"


def get_all_models() -> dict[str, list[str]]:
    """Return models for all known engines."""
    result: dict[str, list[str]] = {}
    for engine in _FALLBACK_MODELS:
        models, _source = get_models(engine)
        if models:
            result[engine] = models
    return result


def invalidate_cache(engine: str | None = None) -> None:
    """Clear cached model lists. Pass ``None`` to clear all."""
    if engine is None:
        _cache.clear()
    else:
        _cache.pop(engine, None)


def find_engine_for_model(model: str) -> str | None:
    """Return the engine ID that owns *model*, or ``None`` if unknown."""
    for engine in _FALLBACK_MODELS:
        models, _ = get_models(engine)
        if model in models:
            return engine
    return None


# Keep KNOWN_MODELS as an alias for backward compatibility in imports
KNOWN_MODELS = _FALLBACK_MODELS


def shorten_model(model: str) -> str:
    """Shorten a model ID for display in status lines.

    ``claude-opus-4-6``        → ``opus4.6``
    ``claude-sonnet-4-5-20250514`` → ``sonnet4.5``
    ``claude-opus-4-20250514`` → ``opus4``
    ``opus``                   → ``opus``  (short aliases unchanged)
    ``o4-mini``                → ``o4-mini`` (non-claude unchanged)
    """
    m = re.sub(r"\[.*\]$", "", model)  # strip context window suffix e.g. [1m]
    m = re.sub(r"-\d{8}$", "", m)  # strip date suffix
    if not m.startswith("claude-"):
        return m
    m = m[len("claude-"):]  # strip "claude-" prefix
    parts = m.split("-")
    name_parts: list[str] = []
    ver_parts: list[str] = []
    for p in parts:
        if p.isdigit():
            ver_parts.append(p)
        elif not ver_parts:
            name_parts.append(p)
    name = "".join(name_parts)
    ver = ".".join(ver_parts)
    return f"{name}{ver}" if ver else name
