from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import anyio
import msgspec

from .utils.json_state import atomic_write_json


class _Logger(Protocol):
    def warning(self, event: str, **fields: Any) -> None: ...


class _VersionedState(Protocol):
    version: int


class JsonStateStore[T: _VersionedState]:
    def __init__(
        self,
        path: Path,
        *,
        version: int,
        state_type: type[T],
        state_factory: Callable[[], T],
        log_prefix: str,
        logger: _Logger,
    ) -> None:
        self._path = path
        self._lock = anyio.Lock()
        self._loaded = False
        self._mtime_ns: int | None = None
        self._state_type = state_type
        self._state_factory = state_factory
        self._version = version
        self._log_prefix = log_prefix
        self._logger = logger
        self._state = state_factory()

    def _stat_mtime_ns(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def _reload_locked_if_needed(self) -> None:
        current = self._stat_mtime_ns()
        if self._loaded and current == self._mtime_ns:
            return
        self._load_locked()

    def _load_locked(self) -> None:
        self._loaded = True
        self._mtime_ns = self._stat_mtime_ns()
        if self._mtime_ns is None:
            self._state = self._state_factory()
            return
        try:
            payload = msgspec.json.decode(
                self._path.read_bytes(), type=self._state_type
            )
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

    def _backup_corrupt(self, reason: str, exc: Exception) -> None:
        """손상 파일을 .corrupt.<timestamp>로 이동하고 경고 로그 기록."""
        ts = int(time.time())
        backup_path = self._path.with_suffix(f"{self._path.suffix}.corrupt.{ts}")
        try:
            os.replace(self._path, backup_path)
        except OSError:
            backup_path = None  # type: ignore[assignment]
        self._logger.warning(
            f"{self._log_prefix}.{reason}",
            path=str(self._path),
            backup=str(backup_path) if backup_path else "failed",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )

    def _save_locked(self) -> None:
        payload = msgspec.to_builtins(self._state)
        atomic_write_json(self._path, payload)
        self._mtime_ns = self._stat_mtime_ns()
