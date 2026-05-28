"""Per-channel preferences store (shared by Mattermost/Slack; Telegram uses its own)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import msgspec
import msgspec.structs

from ..context import RunContext
from ..logging import get_logger
from ..state_store import JsonStateStore

logger = get_logger(__name__)

STATE_VERSION = 1


class Persona(msgspec.Struct, forbid_unknown_fields=False):
    """A reusable persona definition (global, not per-channel)."""

    name: str
    prompt: str


class _ChatPrefs(msgspec.Struct, forbid_unknown_fields=False):
    default_engine: str | None = None
    engine_locked: bool = False  # True: engine change blocked after first run
    trigger_mode: str | None = None  # "all" | "mentions"
    context_project: str | None = None
    context_branch: str | None = None
    engine_models: dict[str, str] = msgspec.field(default_factory=dict)
    engine_reasoning: dict[str, str] = msgspec.field(default_factory=dict)


class _State(msgspec.Struct, forbid_unknown_fields=False):
    version: int = STATE_VERSION
    chats: dict[str, _ChatPrefs] = msgspec.field(default_factory=dict)
    personas: dict[str, Persona] = msgspec.field(default_factory=dict)


def _migrate_telegram_prefs(raw: bytes) -> bytes:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw
    chats = data.get("chats", {})
    if not isinstance(chats, dict):
        return raw
    modified = False
    for chat_val in chats.values():
        if not isinstance(chat_val, dict):
            continue
        if "engine_overrides" in chat_val:
            overrides = chat_val.pop("engine_overrides", {})
            if "engine_models" not in chat_val:
                chat_val["engine_models"] = {}
            if "engine_reasoning" not in chat_val:
                chat_val["engine_reasoning"] = {}
            for eng, ovr in overrides.items():
                if isinstance(ovr, dict):
                    if ovr.get("model"):
                        chat_val["engine_models"][eng] = ovr["model"]
                    if ovr.get("reasoning"):
                        chat_val["engine_reasoning"][eng] = ovr["reasoning"]
            modified = True
    if modified:
        return json.dumps(data).encode("utf-8")
    return raw


class ChatPrefsStore(JsonStateStore[_State]):
    """Persistent per-channel preferences (engine, trigger mode, context)."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            version=STATE_VERSION,
            state_type=_State,
            state_factory=_State,
            log_prefix="chat_prefs",
            logger=logger,
        )

    def _load_locked(self) -> None:
        self._loaded = True
        self._mtime_ns = self._stat_mtime_ns()
        if self._mtime_ns is None:
            self._state = self._state_factory()
            return
        try:
            raw = self._path.read_bytes()
            raw = _migrate_telegram_prefs(raw)
            payload = msgspec.json.decode(raw, type=self._state_type)
        except Exception as exc:  # noqa: BLE001
            self._backup_corrupt("load_failed", exc)
            self._state = self._state_factory()
            return
        if payload.version != self._version:
            self._backup_corrupt(
                "version_mismatch",
                RuntimeError(f"version {payload.version} != {self._version}"),
            )
            self._state = self._state_factory()
            return
        self._state = payload

    def _get(self, channel_id: str | int) -> _ChatPrefs:
        return self._state.chats.get(str(channel_id), _ChatPrefs())

    def _set(self, channel_id: str | int, prefs: _ChatPrefs) -> None:
        key = str(channel_id)
        if prefs == _ChatPrefs():
            self._state.chats.pop(key, None)
        else:
            self._state.chats[key] = prefs

    # -- Public API --

    async def get_default_engine(self, channel_id: str | int) -> str | None:
        async with self._lock:
            self._reload_locked_if_needed()
            return self._get(channel_id).default_engine

    async def set_default_engine(
        self, channel_id: str | int, engine: str | None, *, lock: bool = False
    ) -> None:
        async with self._lock:
            self._reload_locked_if_needed()
            cur = self._get(channel_id)
            prefs = msgspec.structs.replace(
                cur,
                default_engine=engine,
                engine_locked=lock or cur.engine_locked,
            )
            self._set(channel_id, prefs)
            self._save_locked()

    async def is_engine_locked(self, channel_id: str | int) -> bool:
        async with self._lock:
            self._reload_locked_if_needed()
            return self._get(channel_id).engine_locked

    async def lock_engine(self, channel_id: str | int) -> None:
        async with self._lock:
            self._reload_locked_if_needed()
            prefs = msgspec.structs.replace(self._get(channel_id), engine_locked=True)
            self._set(channel_id, prefs)
            self._save_locked()

    async def get_trigger_mode(self, channel_id: str | int) -> str | None:
        async with self._lock:
            self._reload_locked_if_needed()
            return self._get(channel_id).trigger_mode

    async def set_trigger_mode(self, channel_id: str | int, mode: str | None) -> None:
        async with self._lock:
            self._reload_locked_if_needed()
            prefs = msgspec.structs.replace(self._get(channel_id), trigger_mode=mode)
            self._set(channel_id, prefs)
            self._save_locked()

    async def get_context(self, channel_id: str | int) -> RunContext | None:
        async with self._lock:
            self._reload_locked_if_needed()
            prefs = self._get(channel_id)
            if prefs.context_project is None:
                return None
            return RunContext(
                project=prefs.context_project,
                branch=prefs.context_branch,
            )

    async def set_context(
        self, channel_id: str | int, context: RunContext | None
    ) -> None:
        async with self._lock:
            self._reload_locked_if_needed()
            project = context.project if context is not None else None
            branch = context.branch if context is not None else None
            prefs = msgspec.structs.replace(
                self._get(channel_id),
                context_project=project,
                context_branch=branch,
            )
            self._set(channel_id, prefs)
            self._save_locked()

    # -- Per-engine model override API --

    async def get_engine_model(self, channel_id: str | int, engine: str) -> str | None:
        async with self._lock:
            self._reload_locked_if_needed()
            return self._get(channel_id).engine_models.get(engine)

    async def set_engine_model(
        self, channel_id: str | int, engine: str, model: str
    ) -> None:
        async with self._lock:
            self._reload_locked_if_needed()
            prefs = self._get(channel_id)
            models = dict(prefs.engine_models)
            models[engine] = model
            new_prefs = msgspec.structs.replace(prefs, engine_models=models)
            self._set(channel_id, new_prefs)
            self._save_locked()

    async def clear_engine_model(self, channel_id: str | int, engine: str) -> None:
        async with self._lock:
            self._reload_locked_if_needed()
            prefs = self._get(channel_id)
            models = dict(prefs.engine_models)
            if engine in models:
                del models[engine]
                new_prefs = msgspec.structs.replace(prefs, engine_models=models)
                self._set(channel_id, new_prefs)
                self._save_locked()

    async def get_all_engine_models(self, channel_id: str | int) -> dict[str, str]:
        async with self._lock:
            self._reload_locked_if_needed()
            return dict(self._get(channel_id).engine_models)

    # -- Per-engine reasoning override API --

    async def get_engine_reasoning(
        self, channel_id: str | int, engine: str
    ) -> str | None:
        async with self._lock:
            self._reload_locked_if_needed()
            return self._get(channel_id).engine_reasoning.get(engine)

    async def set_engine_reasoning(
        self, channel_id: str | int, engine: str, reasoning: str
    ) -> None:
        async with self._lock:
            self._reload_locked_if_needed()
            prefs = self._get(channel_id)
            reasonings = dict(prefs.engine_reasoning)
            reasonings[engine] = reasoning
            new_prefs = msgspec.structs.replace(prefs, engine_reasoning=reasonings)
            self._set(channel_id, new_prefs)
            self._save_locked()

    async def clear_engine_reasoning(self, channel_id: str | int, engine: str) -> None:
        async with self._lock:
            self._reload_locked_if_needed()
            prefs = self._get(channel_id)
            reasonings = dict(prefs.engine_reasoning)
            if engine in reasonings:
                del reasonings[engine]
                new_prefs = msgspec.structs.replace(prefs, engine_reasoning=reasonings)
                self._set(channel_id, new_prefs)
                self._save_locked()

    async def get_all_engine_reasoning(self, channel_id: str | int) -> dict[str, str]:
        async with self._lock:
            self._reload_locked_if_needed()
            return dict(self._get(channel_id).engine_reasoning)

    # -- Persona API (global, not per-channel) --

    async def get_persona(self, name: str) -> Persona | None:
        async with self._lock:
            self._reload_locked_if_needed()
            return self._state.personas.get(name)

    async def list_personas(self) -> dict[str, Persona]:
        async with self._lock:
            self._reload_locked_if_needed()
            return dict(self._state.personas)

    async def add_persona(self, name: str, prompt: str) -> None:
        async with self._lock:
            self._reload_locked_if_needed()
            self._state.personas[name] = Persona(name=name, prompt=prompt)
            self._save_locked()

    async def remove_persona(self, name: str) -> bool:
        async with self._lock:
            self._reload_locked_if_needed()
            if name not in self._state.personas:
                return False
            del self._state.personas[name]
            self._save_locked()
            return True

    # -- Telegram backward-compatibility helpers --

    async def set_engine_override(
        self, channel_id: str | int, engine: str, override: Any
    ) -> None:
        if override is None:
            await self.clear_engine_model(channel_id, engine)
            await self.clear_engine_reasoning(channel_id, engine)
        else:
            model = getattr(override, "model", None)
            reasoning = getattr(override, "reasoning", None)
            if model is not None:
                await self.set_engine_model(channel_id, engine, model)
            else:
                await self.clear_engine_model(channel_id, engine)

            if reasoning is not None:
                await self.set_engine_reasoning(channel_id, engine, reasoning)
            else:
                await self.clear_engine_reasoning(channel_id, engine)

    async def get_engine_override(
        self, channel_id: str | int, engine: str
    ) -> Any | None:
        model = await self.get_engine_model(channel_id, engine)
        reasoning = await self.get_engine_reasoning(channel_id, engine)
        if model is None and reasoning is None:
            return None
        from ..telegram.engine_overrides import EngineOverrides

        return EngineOverrides(model=model, reasoning=reasoning)

    async def clear_default_engine(self, channel_id: str | int) -> None:
        await self.set_default_engine(channel_id, None)
