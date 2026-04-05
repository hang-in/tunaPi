# Task 03: lockfile-atomic

**Plan:** Operational Resilience & Observability Hardening
**Slug:** `lockfile-atomic`
**Parallel Group:** B
**Depends On:** (none — 독립 모듈)

## Changed Files

| File | Action |
|------|--------|
| `src/tunapi/lockfile.py:67-100` | Modify — `acquire_lock()` 내부를 `O_CREAT \| O_EXCL` 기반으로 전환 |
| `src/tunapi/lockfile.py:128-132` | Modify — `_write_lock_info()` fd 기반 쓰기로 전환 |

## Change Description

현재 `acquire_lock()`의 TOCTOU 경합을 원자적 생성으로 교체한다.

### 현재 흐름 (TOCTOU 취약)

```
_read_lock_info(path)     # 1. 읽기
  → _pid_running(pid)     # 2. 확인   ← 이 사이에 다른 프로세스 진입 가능
_write_lock_info(path)    # 3. 쓰기
```

### 변경 후 흐름

```
os.open(path, O_CREAT | O_EXCL | O_WRONLY)   # 1. 원자적 생성 시도
  → 성공: fd로 lock info 기록 → 완료
  → FileExistsError:
    _read_lock_info(path)                      # 2. 기존 lock 읽기
      → _pid_running(pid)                      # 3. 프로세스 생존 확인
        → 생존: raise LockError
        → 사망(stale): 새 tmp 파일 생성 → os.replace()로 탈취
```

### 구현 상세

```python
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
            # stale lock → 탈취 (replace로 원자적 교체)
            _write_lock_info(
                lock_path,
                pid=os.getpid(),
                token_fingerprint=token_fingerprint,
            )
            return LockHandle(path=lock_path)
        else:
            # fd 획득 성공 — lock info 기록
            _write_lock_info_fd(fd, pid=os.getpid(), token_fingerprint=token_fingerprint)
            return LockHandle(path=lock_path)

    except OSError as exc:
        raise LockError(path=lock_path, state=str(exc)) from exc
```

### 신규 헬퍼 함수

```python
def _write_lock_info_fd(fd: int, *, pid: int, token_fingerprint: str | None) -> None:
    """fd에 직접 lock info를 기록하고 닫는다."""
    payload = {"pid": pid, "token_fingerprint": token_fingerprint}
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
```

### 외부 API 불변

- `acquire_lock()` 시그니처 동일
- `LockHandle` 동일
- `LockError` 동일
- `lock_path_for_config()` 동일

## Dependencies

- 패키지: 없음 (`os` 표준 라이브러리)
- 다른 subtask: 없음 (독립 실행 가능)

## Verification

```bash
# 1. 타입 체크
uv run ty check src/tunapi/lockfile.py

# 2. 기존 lockfile 테스트 통과
uv run pytest tests/ -k "lock" --no-cov -x -q

# 3. O_EXCL 사용 확인
grep -n "O_EXCL" src/tunapi/lockfile.py

# 4. 전체 테스트 (부작용 확인)
uv run pytest tests/ --no-cov -x -q
```

## Risks

- **Windows 호환성:** `O_CREAT | O_EXCL`은 Windows에서도 지원되나, 파일 권한 모드(0o644)가 무시됨. 현재 프로젝트 대상이 macOS/Linux이므로 낮은 리스크.
- **NFS 파일시스템:** NFS에서 `O_EXCL`은 NFSv3까지 원자성이 보장되지 않음. NFSv4+에서는 보장됨. tunapi가 로컬 파일시스템(`~/.tunapi/`)을 사용하므로 해당 없음.
- **stale lock 탈취 경합:** 두 프로세스가 동시에 stale lock을 감지하면 둘 다 `_write_lock_info()`를 호출할 수 있음. `os.replace()`가 원자적이므로 한쪽만 최종 승리하나, 패배측이 에러 없이 진행할 수 있음. 이는 기존 동작과 동일한 수준이며, 완전한 해결은 `fcntl.flock()` 도입이 필요 (이번 범위 외).

## Scope Boundary (수정 금지)

- `LockHandle.release()` — 해제 로직 불변
- `_pid_running()` — PID 확인 로직 불변
- `_format_lock_message()` — 에러 메시지 포맷 불변
