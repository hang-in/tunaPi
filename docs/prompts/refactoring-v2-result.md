# 리팩토링 1차 결과 보고서

> **브랜치**: `refactor/mattermost-v1` (master 기반)
> **실행일**: 2026-03-19
> **프롬프트**: `docs/prompts/refactoring-v2.md`

---

## 요약

| 항목 | 수치 |
|------|------|
| 커밋 수 | 5 (Phase 1~5 각 1개) |
| 변경 파일 | 9개 |
| 추가 줄 | +493 |
| 삭제 줄 | -201 |
| 새 테스트 | 27개 |
| 전체 테스트 | 664 passed |

---

## Phase별 결과

### Phase 1: 에러 바운더리 정책 주석 ✅

`_dispatch_message`와 `_run_single_round`에 에러 처리 정책을 주석으로 명시. 현재 코드가 정책과 일치함을 검증 완료.

- `_dispatch_message`: handle_message 실패 시 "log only" 정책 확인
- `_run_single_round`: 엔진별 실패 시 "warn + skip + continue" 정책 확인

### Phase 2: Roundtable 단위 테스트 ✅

`tests/test_mattermost_roundtable.py` 생성, 27개 테스트 추가.

| 클래스 | 테스트 수 | 대상 |
|--------|----------|------|
| `TestRoundtableStore` | 8 | put/get/complete/remove/evict (TTL 포함) |
| `TestParseRtArgs` | 8 | 토픽 파싱, --rounds 플래그, 에러 케이스 |
| `TestParseFollowupArgs` | 6 | 엔진 필터, 대소문자, 부분 매치 |
| `TestBuildRoundPrompt` | 5 | 컨텍스트 조합, truncation, 구분자 |

선행 작업으로 `_MAX_ANSWER_LENGTH` 상수 추출 (매직 넘버 4000 제거).

### Phase 3: `_dispatch_message` 분해 ✅

310줄 거대 함수를 3개 단계 함수로 분리:

| 함수 | 역할 | 반환 |
|------|------|------|
| `_try_dispatch_command` | 커맨드 파싱 + 디스패치 | `bool` |
| `_resolve_prompt` | 파일/음성/트리거/mention → 정제된 텍스트 | `_ResolvedPrompt \| None` |
| `_run_engine` | 컨텍스트/러너/페르소나/세션 → 엔진 실행 | `None` |

`_dispatch_message`는 3줄 orchestrator로 축소. 외부 시그니처 변경 없음.

### Phase 4: 상태 저장소 JsonStateStore 정렬 ✅

- `telegram/state_store.py` → `tunapi/state_store.py`로 공용 승격
- `mattermost/chat_sessions.py`: `JsonStateStore[_State]` 상속 전환
- `mattermost/chat_prefs.py`: 동일 전환 (10개 메서드 전부)

개선 효과:
- mtime 기반 자동 리로드 (멀티프로세스 일관성)
- atomic write (크래시 시 데이터 손실 방지)
- 버전 불일치 자동 감지 + 로깅

### Phase 5: CommandBackend 정렬 평가 — 스킵 ⏭️

평가 결과: **전환 가능한 핸들러 없음**.

- `CommandContext.executor`가 `run_one`/`run_many`를 요구
- 모든 Mattermost 핸들러가 transport 고유 의존성 필요
- `CommandExecutor` 구현 비용이 전환 이득을 초과
- 별도 작업으로 분리 (코어 `CommandBackend` Protocol 확장 시 재검토)

---

## 남은 작업 (후속 리팩토링 후보)

| 항목 | 우선도 | 비고 |
|------|--------|------|
| `CommandExecutor` 구현 + 핸들러 전환 | 중 | 코어 Protocol 확장 필요 |
| `run_main_loop` SIM117 수정 | 낮음 | nested async with 정리 |
| Roundtable async 함수 통합 테스트 | 중 | `run_roundtable`, `run_followup_round` |
| `_handle_file_command` 분리 | 낮음 | loop.py 내 잔여 대형 함수 |
