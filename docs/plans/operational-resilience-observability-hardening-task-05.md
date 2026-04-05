# Task 05: doctor-diagnostics

**Plan:** Operational Resilience & Observability Hardening
**Slug:** `doctor-diagnostics`
**Parallel Group:** C
**Depends On:** (none — 독립 UX 개선)

## Changed Files

| File | Action |
|------|--------|
| `src/tunapi/cli/doctor.py:30-39` | Modify — `DoctorCheck`에 `suggestion` 필드 추가, `render()` 수정 |
| `src/tunapi/cli/doctor.py:66-114` | Modify — `_doctor_telegram_checks()` 예외 타입별 분류 |
| `src/tunapi/cli/doctor.py:135-171` | Modify — `_doctor_mattermost_checks()` 예외 타입별 분류 |

## Change Description

### 1. `DoctorCheck` 확장

```python
@dataclass(frozen=True, slots=True)
class DoctorCheck:
    label: str
    status: DoctorStatus
    detail: str | None = None
    suggestion: str | None = None   # ← 신규

    def render(self) -> str:
        parts = [f"- {self.label}: {self.status}"]
        if self.detail:
            parts[0] += f" ({self.detail})"
        if self.suggestion:
            parts.append(f"  → {self.suggestion}")
        return "\n".join(parts)
```

**기존 호환성:** `suggestion`은 기본값 `None`이므로 기존 모든 `DoctorCheck(...)` 호출은 변경 불필요.

### 2. `_doctor_telegram_checks()` 예외 분류 (line 110)

현재:
```python
except Exception as exc:
    checks.append(DoctorCheck("telegram", "error", str(exc)))
```

변경 후:
```python
except Exception as exc:
    detail, suggestion = _classify_transport_error(exc, transport="telegram")
    checks.append(DoctorCheck("telegram", "error", detail, suggestion=suggestion))
```

### 3. `_doctor_mattermost_checks()` 예외 분류 (line 168)

동일 패턴 적용:
```python
except Exception as exc:
    detail, suggestion = _classify_transport_error(exc, transport="mattermost")
    checks.append(DoctorCheck("mattermost", "error", detail, suggestion=suggestion))
```

### 4. 공통 분류 헬퍼 (신규)

```python
def _classify_transport_error(
    exc: Exception,
    *,
    transport: str,
) -> tuple[str, str | None]:
    """예외를 사용자 친화적 메시지와 수정 가이드로 분류."""
    import httpx

    error_str = str(exc)

    # Timeout
    if isinstance(exc, httpx.TimeoutException):
        return (
            f"timeout ({error_str[:80]})",
            f"{transport} 서버가 응답하지 않습니다. URL과 네트워크 연결을 확인하세요.",
        )

    # Connection error (DNS, refused, etc.)
    if isinstance(exc, httpx.ConnectError):
        return (
            f"connection failed ({error_str[:80]})",
            f"{transport} 서버에 연결할 수 없습니다. URL, DNS, 방화벽을 확인하세요.",
        )

    # Auth errors (HTTP 401/403 or known patterns)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return (
                f"auth failed (HTTP {status})",
                "토큰이 만료되었거나 잘못되었습니다. 토큰을 재발급하세요.",
            )
        return (
            f"HTTP {status} ({error_str[:80]})",
            None,
        )

    # Fallback — 기존 동작과 동일
    return (error_str[:120], None)
```

### 5. 기존 suggestion 추가 (정적 체크)

이미 구체적 에러를 알고 있는 기존 체크에 suggestion 추가:

```python
# voice transcription API key 미설정 (line 63)
DoctorCheck(
    "voice transcription", "error", "API key not set",
    suggestion="OPENAI_API_KEY 환경변수를 설정하거나 voice_transcription_api_key를 tunapi.toml에 추가하세요.",
)

# telegram token invalid (line 82-83)
DoctorCheck(
    "telegram token", "error", "failed to fetch bot info",
    suggestion="TELEGRAM_TOKEN이 유효한지 BotFather에서 확인하세요.",
)
```

**참고:** Slack doctor(`_doctor_slack_checks`)는 이미 line 256-265에서 에러 분류를 하고 있으므로 `suggestion` 추가만 진행.

## Dependencies

- 패키지: `httpx` (이미 프로젝트 의존성)
- 다른 subtask: 없음

## Verification

```bash
# 1. 타입 체크
uv run ty check src/tunapi/cli/doctor.py

# 2. doctor 관련 테스트
uv run pytest tests/ -k "doctor" --no-cov -x -q

# 3. suggestion 필드 존재 확인
grep -n "suggestion" src/tunapi/cli/doctor.py

# 4. _classify_transport_error 존재 확인
grep -n "_classify_transport_error" src/tunapi/cli/doctor.py

# 5. 전체 테스트
uv run pytest tests/ --no-cov -x -q
```

## Risks

- **httpx import:** `_classify_transport_error` 내에서 `import httpx`를 사용. doctor.py가 이미 간접적으로 httpx를 사용하므로 문제 없음. 단, `httpx.HTTPStatusError`가 발생하려면 transport client가 `raise_for_status()`를 호출해야 하는데, 현재 코드에서 이를 하지 않을 수 있음. 이 경우 해당 분기는 도달하지 않지만 해롭지 않음.
- **테스트 render() 출력 변경:** `render()` 메서드가 `suggestion` 있을 때 2줄 출력으로 바뀜. 기존 테스트가 `render()` 출력을 exact match하면 실패 가능. `suggestion=None`이 기본이므로 기존 테스트에 영향 없음.

## Scope Boundary (수정 금지)

- `_doctor_slack_checks()` — 이미 에러 분류 구현됨. `suggestion` 추가만 허용하되 기존 분류 로직 변경 금지.
- `run_doctor()` — 오케스트레이션 로직 불변
- `_resolve_cli_attr()` — 테스트 유틸리티 불변
