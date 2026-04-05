# Task 01: fsync-atomic-write

**Plan:** Operational Resilience & Observability Hardening
**Slug:** `fsync-atomic-write`
**Parallel Group:** A
**Depends On:** (none — first task)

## Changed Files

| File | Action |
|------|--------|
| `src/tunapi/utils/json_state.py:18-21` | Modify — add `flush()` + `os.fsync()` before `os.replace()` |

## Change Description

`atomic_write_json()` 함수에서 `os.replace()` 호출 전에 `handle.flush()` + `os.fsync(handle.fileno())`를 추가한다.

**현재 코드** (`json_state.py:18-21`):
```python
with open(tmp_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=indent, sort_keys=sort_keys)
    handle.write("\n")
os.replace(tmp_path, path)
```

**변경 후:**
```python
with open(tmp_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=indent, sort_keys=sort_keys)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(tmp_path, path)
```

**이유:** 전원 손실 시 OS가 아직 디스크에 기록하지 않은 버퍼 데이터를 잃을 수 있다. `os.replace()`는 메타데이터만 원자적이며 내용 flush를 보장하지 않는다. 동일 프로젝트의 `config.py:152-153`에서는 이미 이 패턴을 사용하므로 일관성도 확보된다.

### 수혜 호출처 (자동 적용)

1. `src/tunapi/state_store.py:87` — `JsonStateStore._save_locked()` → 7개 서브클래스
2. `src/tunapi/journal.py:389` — `PendingRunLedger._save()`
3. `src/tunapi/core/roundtable.py:131` — `RoundtableStore._save()`

## Dependencies

- 패키지: 없음 (`os` 표준 라이브러리)
- 다른 subtask: 없음

## Verification

```bash
# 1. 타입 체크
uv run ty check src/tunapi/utils/json_state.py

# 2. 기존 테스트 통과 확인
uv run pytest tests/ --no-cov -x -q

# 3. fsync 호출 존재 확인
grep -n "os.fsync" src/tunapi/utils/json_state.py

# 4. config.py와 패턴 일치 확인
grep -A1 "flush" src/tunapi/utils/json_state.py src/tunapi/config.py
```

## Risks

- **성능:** fsync는 디스크 I/O를 강제하므로 SSD 기준 ~1ms 지연 추가. 상태 저장 빈도가 낮아 (세션 변경 시만) 무시할 수 있는 수준.
- **호환성:** `os.fsync()`는 POSIX 표준. Windows에서도 `_commit()`으로 매핑되어 동작함.

## Scope Boundary (수정 금지)

- `src/tunapi/discord/state.py` — Discord 전용 `_atomic_write_json` 복사본은 이번 범위 외
- `src/tunapi/discord/prefs.py` — 동일
