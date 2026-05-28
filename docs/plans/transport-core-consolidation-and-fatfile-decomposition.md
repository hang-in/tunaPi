---
name: transport-core-consolidation-and-fatfile-decomposition
title: Transport Core 통합 및 비대 파일 분해 리팩토링
status: draft
priority: P1
created: 2026-05-28
owner: unassigned
related_plans:
  - core-extraction-refactor.md
  - operational-resilience-observability-hardening.md
  - transport-test-coverage-plan.md
---

# Transport Core 통합 및 비대 파일 분해 리팩토링

## 0. 요약 (TL;DR)

5개 transport 중 **Mattermost/Slack은 `core/` 위로 잘 올라갔고, Telegram/Discord/Tunadish는 그대로 남았다**. 동시에 단일 파일이 1k~2k 라인까지 부풀어 있다. 본 계획은 다음을 동시에 푼다.

1. **MM/Slack `loop.py`의 쌍둥이 헬퍼**(~1.1k LoC × 2)를 `core/`로 마지막 단계 추출
2. **MM/Slack/Tunadish `commands.py`의 13개 핸들러**를 `core/commands/`로 통합 (transport별 차이는 콜백 주입)
3. **Telegram/Discord의 자체 outbox·chat_prefs·chat_sessions를 core 기반으로 마이그레이션**
4. **비대 파일 4개**(`discord/loop.py`, `discord/handlers.py`, `tunadish/backend.py`, `tunadish/commands.py`) **함수 단위 분해**
5. **테스트 페이크 모듈화**(Slack/MM/Discord/Tunadish용 `*_fakes.py` 추가) + **테스트 무결성 정리**(`test_coverage_push.py` 3792줄을 기능별로 재배치)

목표는 **추상화 신설이 아니라 기존 `core/` 자산을 끝까지 활용**하는 것이다. 새 미들웨어/플러그인 시스템은 도입하지 않는다.

---

## 1. Invariants (불변 조건)

- **외부 동작 무변경**: 채팅 사용자 입장에서 명령어 응답·메시지 포맷·에러 메시지가 동일해야 한다.
- **모든 변경은 기능 플래그 없이 in-place 리팩토링**. 호환성 shim은 추출 직후 단일 PR 안에서 제거한다(긴 deprecation 금지).
- 각 서브태스크 후 `just check` (= ruff + ty + pytest) 통과. **`--cov-fail-under=81` 유지**, 가능하면 상향.
- `core/` 내부에서 `if transport == "..."` 분기 금지. transport 차이는 콜백/정책 객체 주입으로 처리한다 (기존 `core-extraction-refactor.md` 규칙 승계).
- 한 PR 당 한 transport × 한 관심사만 다룬다 (예: "Telegram outbox만 마이그레이션").
- pyproject `entry_points` 변경 금지 (백엔드 ID 호환).

## 2. Goals

기능/역할·책임/추상화/코드품질/테스트 커버리지 5축으로 정리한다.

### G1. 기능 (Feature Parity)
- 5개 transport가 동일 명령 세트(`/help`, `/model`, `/trigger`, `/status`, `/cancel`, `/new`, `/project`, `/persona`, `/rt` + 메모리/브랜치/리뷰)에 대해 **공통 핸들러 1벌**로 응답한다.
- Telegram에 부분 누락된 conversation branching이 본 계획 범위에서 추가되지는 않지만, 통합으로 인해 **장래 도입 비용이 떨어지는** 구조가 된다.

### G2. 역할 / 책임 (Module Responsibility)
- `core/`: transport 무관 비즈니스 로직 + 상태 (현재대로)
- `<transport>/loop.py`: 외부 이벤트 수신 → 정규화 → core 호출 → 응답 전송 **루프만**. ≤ 600 LoC.
- `<transport>/commands.py`: 명령 라우팅 + transport 고유 응답 포맷 **콜백만**. ≤ 400 LoC.
- `<transport>/bridge.py` + `presenter.py`: `Transport`/`Presenter` 프로토콜 구현체 only.
- 단일 파일은 **800 LoC 미만**을 권장선으로 한다(roundtable은 예외 허용).

### G3. 추상화 (Abstraction Boundaries)
- `core.ChatPresenter`를 Discord/Telegram/Tunadish가 **확장**하거나, 확장 못 하는 정당한 이유를 코드 주석으로 남긴다.
- `core.Outbox`를 모든 transport가 사용한다 (Telegram/Discord는 콜백으로 chat-aware rate limiting 주입).
- `core.ChatPrefsStore` / `core.ChatSessionStore` 단일 소스. Telegram이 갖고 있던 별도 구현은 제거하거나 core API에 흡수.
- 어떤 모듈도 `core` ← `transport` 의존성 금지 (현재 ✅ — 회귀 방지 테스트 추가).

### G4. 코드 품질 (Quality)
- 800 LoC 초과 파일 4개를 **각각 3~5개 모듈로 분해**.
- 200 LoC 초과 함수(특히 `discord/loop.py`의 `handle_message`, `discord/handlers.py`의 `_handle_ctx_command`, `_handle_engine_command`)를 80 LoC 이하 helper로 분해.
- `except Exception:` 광범위 블록 검토 (commands.py 다수). 의미 있는 예외 클래스로 좁히거나 로그 + 재발생.
- 매직 넘버 (`MAX_POST_LENGTH = 16383`, `DEFAULT_CHANNEL_INTERVAL = 0.2`) 상수에 docstring 추가.

### G5. 테스트 커버리지 (Test)
- **무커버리지 모듈** `events.py`, `markdown.py`, `runner_bridge.py`, `scheduler.py`, `codex_events.py`, `gemini.py`, `runners/mock.py`에 단위 테스트 추가 (각 ≥ 60% 라인).
- `tests/fakes/`를 신설하고 Slack/MM/Discord/Tunadish 페이크를 분리 (`telegram_fakes.py` 패턴 복제).
- `test_coverage_push.py`(3792 LoC) 클래스 단위로 쪼개 적절한 `test_<feature>.py`에 이주.
- `--cov-fail-under` 단계적으로 **81 → 83 → 85**로 상향.

## 3. Non-Goals

- Outbox heapq 알고리즘 재설계 (현 큐 크기에서 불필요)
- Runner mixin → composition 전환 (별도 plan 필요; 이전 plan에서도 제외)
- 새 transport 추가 (Mastodon/Matrix 등)
- 새 engine 추가
- 프롬프트 인젝션 방어
- Python 버전 매트릭스 정리
- README/docs 다국어 동기화 (별도 작업)
- conversation branching의 Telegram 도입 (별도 product 결정 필요)
- `commands` 서브 패키지화를 모든 transport에 강제하기 (Telegram만 분리됨 — 그대로 둔다)

## 4. Subtask 구성

10개 서브태스크. 의존 그래프 기반 4개 페이즈로 진행하며, 각 페이즈 내부는 병렬 가능.

| # | Slug | Title | 의존 | 페이즈 | 예상 LoC 영향 |
|---|------|-------|------|--------|-----------------|
| 01 | `dep-graph-guardrails` | `core → transport` 역방향 import 금지 회귀 테스트 + 측정 베이스라인 기록 | — | A | +50 / 0 |
| 02 | `mm-slack-loop-twin-extract` | MM/Slack `loop.py` 쌍둥이 헬퍼(`_dispatch_rt_command`, `_resolve_persona_prefix`, `_auto_bind_channel_project`, `_send_to_channel`, `_handle_cancel_reaction`, `_handle_voice`, `_handle_file_command`, `_resolve_prompt`, `_run_engine`) → `core/chat_loop_helpers.py` | 01 | B | −800 |
| 03 | `mm-slack-tunadish-commands-unify` | 13개 명령 핸들러를 `core/commands/handlers/`로 추출 (transport별 formatter 콜백 주입) | 01 | B | −1500 |
| 04 | `telegram-state-migrate-to-core` | Telegram `chat_prefs`, `chat_sessions`, `outbox`를 core 기반 shim으로 전환. 필요 시 core API 확장 (file 단위 schema 옵션) | 01 | B | −400 |
| 05 | `discord-state-migrate-to-core` | Discord `outbox`, `prefs`, `state`를 core 기반으로 마이그레이션. 채널 단위 rate limit 콜백 주입 | 01 | B | −300 |
| 06 | `discord-handle-message-split` | `discord/loop.py:handle_message`(~700 LoC) → `_route_message` / `_resolve_intent` / `_send_results` 3개 함수로 분해. handlers.py의 `_handle_ctx_command`, `_handle_engine_command` 동시 분해 | 02 | C | −0 (이동만) |
| 07 | `tunadish-backend-split` | `tunadish/backend.py`(1879) → `backend.py` (server lifecycle) / `rpc_dispatch.py` (JSON-RPC) / `run_exec.py` (`_execute_run`) / `session_glue.py` 4개로 분해 | 03 | C | −0 (이동만) |
| 08 | `presenter-unify-or-document` | Discord/Telegram/Tunadish의 자체 Presenter를 `core.ChatPresenter` 확장으로 전환. 불가능한 경우 코드에 사유 주석 명시 | 02, 03 | C | −150 |
| 09 | `test-fakes-extraction` | `tests/fakes/{slack,mattermost,discord,tunadish}.py` 신설 + 인라인 페이크 마이그레이션 | 02~07 | D | +200 / −400 |
| 10 | `coverage-gap-and-push-cleanup` | `events.py`/`markdown.py`/`runner_bridge.py`/`scheduler.py`/`codex_events.py`/`gemini.py`/`mock.py` 테스트 추가, `test_coverage_push.py` 클래스 분산 이주, `--cov-fail-under` 83 → 85 단계 상향 | 09 | D | +1500 테스트 |

**실행 순서**: A → B(02·03·04·05 병렬) → C(06·07·08 병렬) → D(09 → 10)

각 서브태스크는 별도 PR. PR마다 다음을 포함:
- 변경 파일/라인 요약
- LoC delta 측정 (`tokei` 또는 `wc -l`)
- `just check` 통과 스크린샷 또는 로그
- (관련 시) `--cov-fail-under` 변경 사유

## 5. Verification Strategy

### 정적
- `ruff check src/ tests/` → 신규 위반 0
- `ty check` → pass
- 회귀 import 테스트 (Task 01에서 생성): `tests/test_layering.py`에서 `core/` 모듈이 `tunapi.telegram`, `tunapi.discord`, `tunapi.slack`, `tunapi.mattermost`, `tunapi.tunadish` import하지 않음을 AST로 검증
- 파일 크기 회귀 테스트: `tests/test_file_sizes.py`에 800 LoC 초과 파일 화이트리스트 명시(점진 축소)

### 동적
- `uv run pytest --no-cov` → 3,538 items 전부 pass (각 PR마다)
- `uv run pytest --cov=tunapi --cov-branch` → 커버리지 ≥ 현재값. Phase D 종료 시점에 85% 이상
- 수동 smoke (PR 단위로 의무는 아님, 페이즈 종료 시):
  - MM/Slack/Discord/Telegram/Tunadish 각 1개 채널에서 `/help`, `/model`, `!rt "hello"` 응답 확인
  - 세션 resume (메시지 보내고 끊었다가 재접속) 확인

### 측정 베이스라인 (Task 01에서 기록)
- 전체 src LoC: 47,167
- 800 LoC 초과 파일 수: 13개 (목표: ≤ 6)
- `core` 모듈 transport별 사용량 (import 카운트):
  - MM 14 / Slack 13 / Discord 2 / Telegram 3 / Tunadish 측정 필요
- pytest items: 3,538 / 커버리지: 현재 측정값(예: 81~82%)

## 6. Risks & Mitigations

| 위험 | 영향 | 완화 |
|------|------|------|
| Telegram 자체 chat_prefs/sessions가 core와 schema 호환 안 됨 | Task 04 막힘 | 마이그레이션 스크립트 + 두 스키마 모두 읽도록 core 확장 |
| MM/Slack 쌍둥이 헬퍼 안에 미묘한 transport 차이 | 회귀 | 추출 전 두 파일을 diff로 라인 단위 비교, 차이를 함수 인자로 명시 |
| Discord `handle_message` 분해 시 control flow 변경 | 메시지 누락/중복 | 단순 추출만(extract method); 분기 합치기/제거 금지. 분해 후 기존 테스트 통과 필수 |
| Tunadish backend 분해 후 JSON-RPC 메서드 시그니처 변경 | 웹 클라이언트 호환성 | 외부 메서드명·파라미터 동결, 내부 함수 이동만 |
| `test_coverage_push.py` 이주 중 일부 테스트가 사실은 다른 모듈 검사 중이었음이 드러남 | 커버리지 일시 하락 | 이주 PR에서 누락분 보충 후 머지. `--cov-fail-under` 일시 하향 금지(이주 끝나고 상향만) |
| Presenter 통합이 형식 차이를 깨뜨림 (Slack mrkdwn vs MM markdown vs Discord embed) | 사용자 가시 버그 | Task 08는 "확장 가능한 것만 확장". 차이가 큰 transport는 그대로 두고 사유 주석 |

## 7. Out-of-band 영향

- `docs/plans/index.md`에 본 plan 등록.
- `CLAUDE.md` Architecture 섹션: Telegram/Discord 마이그레이션 완료 후 "별도 구현 유지" 문구 갱신.
- 메모리 [[project_architecture]]의 "Migration to core is partial" 문단을 Phase B/C 완료 시점에 갱신.
- README "테스트: 3,538개 / 커버리지: 81%" 행은 Phase D 종료 후 상향된 값으로 갱신.

## 8. 후속 검토 트리거

다음 중 하나라도 해당되면 plan 재검토:
- Task 02 시작 시 MM/Slack helper diff가 10라인 이상 진짜로 다름 (추출 전략 재설계 필요)
- Task 04에서 Telegram chat_sessions schema가 core v2와 호환 불가
- 새 transport 추가 결정 (Matrix 등) — 본 계획의 추상화 적정성 재평가
- runner 계층 리팩토링 결정 — Task 10이 그쪽 모듈 커버리지에 영향
