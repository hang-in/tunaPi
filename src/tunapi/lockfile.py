from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LockInfo:
    pid: int | None
    token_fingerprint: str | None


class LockError(RuntimeError):
    def __init__(
        self,
        *,
        path: Path,
        state: str,
    ) -> None:
        self.path = path
        self.state = state
        super().__init__(_format_lock_message(path, state))


@dataclass(slots=True)
class LockHandle:
    path: Path

    def release(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "lock.release.failed",
                path=str(self.path),
                error=str(exc),
                error_type=exc.__class__.__name__,
            )

    def __enter__(self) -> LockHandle:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def token_fingerprint(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return digest[:10]


def lock_path_for_config(config_path: Path, *, transport_id: str | None = None) -> Path:
    if transport_id:
        return config_path.with_suffix(f".{transport_id}.lock")
    return config_path.with_suffix(".lock")


def acquire_lock(
    *,
    config_path: Path,
    token_fingerprint: str | None = None,
    transport_id: str | None = None,
) -> LockHandle:
    cfg_path = config_path.expanduser().resolve()
    lock_path = lock_path_for_config(cfg_path, transport_id=transport_id)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # 1. 원자적 생성 시도
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            # 2. 기존 lock 확인
            existing = _read_lock_info(lock_path)
            if existing:
                # token fingerprint 변경: 동일 사용자의 설정 변경으로 간주
                if (
                    token_fingerprint
                    and existing.token_fingerprint
                    and existing.token_fingerprint != token_fingerprint
                ):
                    _write_lock_info(
                        lock_path,
                        pid=os.getpid(),
                        token_fingerprint=token_fingerprint,
                    )
                    return LockHandle(path=lock_path)
                # PID 확인
                if _pid_running(existing.pid):
                    raise LockError(path=lock_path, state="running") from None
            # stale lock → 탈취 (tmp 파일 생성 후 os.replace()로 원자적 교체)
            tmp_path = lock_path.with_suffix(f"{lock_path.suffix}.{os.getpid()}.tmp")
            _write_lock_info(
                tmp_path,
                pid=os.getpid(),
                token_fingerprint=token_fingerprint,
            )
            os.replace(str(tmp_path), str(lock_path))
            # 두 프로세스가 동시에 stale lock을 탈취하면 os.replace() 중 하나만 최종 승리.
            # 재검증: 파일을 다시 읽어 현재 PID가 최종 소유자인지 확인.
            verified = _read_lock_info(lock_path)
            if verified is None or verified.pid != os.getpid():
                raise LockError(path=lock_path, state="running") from None
            return LockHandle(path=lock_path)
        else:
            # fd 획득 성공 — lock info 기록
            _write_lock_info_fd(
                fd, pid=os.getpid(), token_fingerprint=token_fingerprint
            )
            return LockHandle(path=lock_path)

    except OSError as exc:
        raise LockError(path=lock_path, state=str(exc)) from exc

    return LockHandle(path=lock_path)


def _read_lock_info(path: Path) -> LockInfo | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        pid = None
    token_hint = data.get("token_fingerprint")
    if not isinstance(token_hint, str):
        token_hint = None
    return LockInfo(
        pid=pid,
        token_fingerprint=token_hint,
    )


def _write_lock_info(path: Path, *, pid: int, token_fingerprint: str | None) -> None:
    payload = {"pid": pid, "token_fingerprint": token_fingerprint}
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_lock_info_fd(fd: int, *, pid: int, token_fingerprint: str | None) -> None:
    """fd에 직접 lock info를 기록하고 닫는다."""
    payload = {"pid": pid, "token_fingerprint": token_fingerprint}
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _pid_running(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _format_lock_message(path: Path, state: str) -> str:
    if state != "running":
        return f"error: lock failed: {state}"
    header = "error: already running"
    display_path = _display_lock_path(path)
    lines = [header, f"remove {display_path} if stale"]
    return "\n".join(lines)


def _display_lock_path(path: Path) -> str:
    home = Path.home()
    try:
        resolved = path.expanduser().resolve()
        rel = resolved.relative_to(home)
        return f"~/{rel}"
    except (ValueError, OSError):
        return str(path)
