# Task 02: state-corruption-backup

**Plan:** Operational Resilience & Observability Hardening
**Slug:** `state-corruption-backup`
**Parallel Group:** B
**Depends On:** Task 01 (fsync가 있어야 백업 파일도 안전하게 기록됨)

## Changed Files

| File | Action |
|------|--------|
| `src/tunapi/state_store.py:55-82` | Modify — decode/version 실패 시 `.corrupt` 백업 후 리셋 |
| `src/tunapi/journal.py:70-85` | Modify — append 실패 카운터 + 임계값 에러 로그 |

## Change Description

### 1. `state_store.py` — JsonStateStore._load_locked() 수정

**현재 동작:** decode 실패·버전 불일치 시 `self._state = self._state_factory()` (무음 리셋)

**변경 후:**
- decode 실패 또는 버전 불일치 시:
  1. 기존 파일을 `{path}.corrupt.{timestamp}` 로 이동 (`os.replace`)
  2. warning 로그에 **백업 경로** 포함
  3. 이후 기존 동작대로 빈 state 생성

```python
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
    except Exception as exc:
        self._backup_corrupt("decode_failed", exc)
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
    import time
    ts = int(time.time())
    backup_path = self._path.with_suffix(f"{self._path.suffix}.corrupt.{ts}")
    try:
        os.replace(self._path, backup_path)
    except OSError:
        backup_path = None  # 이동 실패 시에도 계속 진행
    self._logger.warning(
        f"{self._log_prefix}.{reason}",
        path=str(self._path),
        backup=str(backup_path) if backup_path else "failed",
        error=str(exc),
        error_type=exc.__class__.__name__,
    )
```

**상단 import 추가:** `import os`, `import time` (os는 이미 간접 사용, time은 신규)

### 2. `journal.py` — Journal.append() 실패 카운터

**현재 동작:** append 실패 시 `logger.warning` 1회만

**변경 후:**
- `Journal.__init__`에 `self._append_failures: int = 0` 추가
- append 실패 시 카운터 증가
- 임계값(3회) 초과 시 `logger.error`로 승격
- 성공 시 카운터 리셋

```python
async def append(self, entry: JournalEntry) -> None:
    path = self._path_for(entry.channel_id)
    line = _encoder.encode(entry) + b"\n"
    async with self._lock:
        try:
            async with await anyio.open_file(path, "ab") as f:
                await f.write(line)
            await self._maybe_rotate(path)
            self._append_failures = 0  # 성공 시 리셋
        except Exception as exc:
            self._append_failures += 1
            log_fn = logger.error if self._append_failures >= 3 else logger.warning
            log_fn(
                "journal.append_failed",
                channel_id=entry.channel_id,
                error=str(exc),
                consecutive_failures=self._append_failures,
            )
```

## Dependencies

- 패키지: 없음 (표준 라이브러리만 사용)
- Task 01: fsync 완료 후 적용 (백업 파일 기록 시 fsync 경유)

## Verification

```bash
# 1. 타입 체크
uv run ty check src/tunapi/state_store.py src/tunapi/journal.py

# 2. 기존 테스트 통과
uv run pytest tests/ --no-cov -x -q

# 3. _backup_corrupt 메서드 존재 확인
grep -n "_backup_corrupt" src/tunapi/state_store.py

# 4. _append_failures 필드 존재 확인
grep -n "_append_failures" src/tunapi/journal.py

# 5. state_store 관련 테스트
uv run pytest tests/ -k "state_store or json_state or chat_session or chat_pref" --no-cov -x -q
```

## Risks

- **`.corrupt` 파일 누적:** 장기 운영 시 디스크 점유 가능. 향후 cleanup 정책 (TTL 또는 max count) 추가 필요하나 이번 범위 아님.
- **os.replace 실패:** 권한 문제 시 백업 이동이 실패할 수 있으나, `except OSError`로 처리하고 원래 동작(빈 state 생성)은 유지됨.
- **Journal 에러 로그 승격:** 운영 알림 시스템이 `error` 레벨을 감지하는 경우 노이즈가 될 수 있으나, 3회 연속 실패는 실제 문제 징후이므로 적절함.

## Scope Boundary (수정 금지)

- `src/tunapi/state_store.py`의 `_save_locked()` — 저장 로직은 변경하지 않음 (Task 01에서 fsync 추가됨)
- `src/tunapi/journal.py`의 `recent_entries()`, `_maybe_rotate()` — 읽기/회전 로직 불변
- `JsonStateStore` 서브클래스 7개 — base class 변경으로 자동 적용, 개별 수정 불필요
