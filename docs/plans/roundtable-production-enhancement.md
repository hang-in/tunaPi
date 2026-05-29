---
name: roundtable-production-enhancement
title: Roundtable(!rt) 프로덕션 고도화 — without bloat
status: draft
priority: P1
created: 2026-05-29
owner: unassigned
related_plans:
  - roundtable-evolution-for-tunadish.md
  - transport-core-consolidation-and-fatfile-decomposition.md
---

# Roundtable(!rt) 프로덕션 고도화 — without bloat

## 0. 요약 (TL;DR)

tunapi의 `!rt`를 "엔진 N개 순차 의견 수집 + concat"에서 **role 기반 구조화 토론**으로 끌어올린다. 큰형님 **tunaFlow**의 검증된 멀티에이전트 패턴(role 지시문/blind verifier/vote 집계/consensus 마커/cross-round consensus 주입/doom-loop 정지)을 **순수 함수로만** 포팅한다.

**핵심 통찰 — tunapi는 이미 절반을 만들어두고 안 쓰고 있다.** 실행층(`core/roundtable.py`)은 flat transcript만 쓰고, 구조화층(`rt_participant`/`rt_utterance`/`rt_structured`/`synthesis`/`discussion_records`)은 role·phase·reply_to·synthesis 필드를 다 갖췄지만 **persist 시점에만 lossy하게 채워지고 실행 루프는 절대 읽지 않는다**(role==engine, phase 전부 "opinion", synthesis는 summary→thesis 복사만). 따라서 고도화 = **새 서브시스템 신설이 아니라 (1) dormant 구조화층을 실행에 연결 + (2) tunaFlow 순수 패턴 포팅 + (3) 비대한 `roundtable.py`(717 LoC) 분해.**

tunaFlow 자체 로드맵이 순서를 못박는다: **출력계약(role) → 집계(vote) → 종합(synthesis).** 거꾸로 하면 synthesis가 "그럴듯한 덮어쓰기"만 된다.

---

## 1. Invariants (불변 조건)

- **외부 동작 호환**: 기존 `!rt "topic" --rounds N` 사용자 입장 동작은 유지(role 미지정 시 현행과 동등한 결과). 새 기능은 opt-in 또는 점진 활성화.
- **새 영속화 추가 금지** — 기존 `rt_structured`/`synthesis`/`discussion_records`/`conversation_branch` 재사용. DB·임베딩·인덱스 도입 금지.
- **순수 함수 우선** — roles/aggregate/consensus/convergence는 transport·IO 의존 0의 순수 모듈로. 결합도 최소, 단위 테스트 trivial.
- **lean 규율**: 신규 rt 모듈 파일은 **≤250 LoC** 자율선(`tests/test_file_sizes.py` <800 가드레일 위에). `core/roundtable.py`는 분해 후 더 커지지 않는다.
- 각 서브태스크 후 `just check` 통과, `--cov-fail-under=83` 유지.
- `core/` 내부 `if transport == "..."` 분기 금지(콜백 주입 규칙 승계).

## 2. Goals

### G1. 기능 (Feature)
- **Role 타입**(proposer/reviewer/verifier/synthesizer)이 실행에 반영 — 지시문 + role별 토큰캡 프롬프트 주입. dormant `RoundtableParticipant.role/instruction/model_override` 활성화.
- **Blind verifier** — 특정 참가자는 topic만 받아 독립 판단(편향↓).
- **구조화 synthesis** — synthesizer 라운드가 vote tally/consensus/contested/dissent를 machine-readable 마커로 산출 → 기존 `SynthesisArtifact`(thesis/agreements/disagreements/open_questions)를 **실제로 채움**.
- **Cross-round consensus 주입** — 다음 라운드 프롬프트에 "여기까진 합의됨, 재론 금지" 누적 주입(멀티라운드 품질 최대 레버).
- **수렴/정지** — doom-loop 감지(fail 누적·findings 중복)로 무한 토론 방지 + escalate 신호.

### G2. 역할/책임 (Module)
- `core/roundtable/` 패키지로 분해: `orchestrator.py`(라운드 루프), `prompt.py`(프롬프트 빌드+consensus 주입), `store.py`(세션 저장), `roles.py`(순수: 지시문+토큰캡), `aggregate.py`(순수: vote tally), `consensus.py`(순수: 마커 추출), `convergence.py`(순수: doom-loop), `commands.py`(파싱/디스패치).
- tunaFlow `roundtable_helpers/` 분해를 미러(types/prompt/persist/sequential/deliberative/executor).

### G3. 추상화
- 구조화층(rt_structured/synthesis/discussion)을 **실행 시점에 build/read** — 더는 persist-only 아카이브가 아니게.
- 4개 구조화 store의 동일 boilerplate(`_store_for`+`JsonStateStore`+`_State`)를 공통 base로 흡수(중복 제거).

### G4. 코드 품질
- `core/roundtable.py`(717) → 패키지 분해, 중복 round-runner(`_run_round_parallel`/`_run_single_round` ~90줄 중복) 통합.
- `_now_iso()` 4+곳 중복 제거.
- `memory_facade`의 타 store private(`_lock`/`_state`/`_save_locked`) 접근 제거 → 공개 API로.
- 구조화/synthesis 저장의 blanket `suppress(Exception)` → 로깅(묵살 금지).

### G5. 테스트/패리티
- 순수 모듈(roles/aggregate/consensus/convergence)은 ≥90% 커버 단위 테스트.
- **transport 패리티**: tunaDish rt(현 stub) 구현, Discord 영속화(현 미저장), Telegram 배선을 공통 `dispatch_roundtable_command_flow`로 통일.

## 3. Non-Goals (tunaFlow에서 포팅 금지 — 브릿지 비대화)
- 임베딩/벡터 인덱스(`RtVectorIndex`, rawq 임베더) — truncation(4000자)으로 충분.
- context cache(plans/subtasks/failure_lessons 주입) — app 전용.
- OTel trace span, Tauri transport(`app.emit`), agent_jobs 잡 레코드.
- plan→implement→review 워크플로우 전체(별도 product 기능) — 순수 헬퍼(verdict 집계/doom-loop)만 채굴.
- 로컬엔진(vLLM/ollama) 라우팅 분기.

## 4. Subtask 구성

순서 강제: **P0(분해) → P1(role) → P2(vote+synthesis) → P3(consensus+수렴) → P4(패리티/위생).** 각 별도 PR, `just check` 통과.

| # | Slug | Title | 의존 | 비고 |
|---|------|-------|------|------|
| 01 | `rt-package-split` | `core/roundtable.py`(717) → `core/roundtable/` 패키지 분해(orchestrator/prompt/store/commands), 중복 round-runner 통합. 기능 무변경 | — | 분해만, extract-method. 기존 테스트 통과 필수 |
| 02 | `rt-roles` | `roles.py`(순수): role_guidance + 토큰캡 포팅. 참가자에 role 부여, 지시문/캡을 prompt·runner에 주입. dormant 구조화층 실행 연결 | 01 | tunaLlama 위임 후보(순수) |
| 03 | `rt-blind-verifier` | blind 참가자는 topic-only 프롬프트 | 02 | 소규모 |
| 04 | `rt-vote-aggregate` | `aggregate.py`(순수): 보수적 tally + 차원별 stddev 쟁점탐지 | 02 | tunaLlama 위임 후보 |
| 05 | `rt-consensus-marker` | `consensus.py`(순수): `<!-- consensus -->` JSON fence + md fallback 추출 → SynthesisArtifact 실채움. synthesizer 라운드(≥2 reviewer) | 04 | tunaLlama 위임 후보 |
| 06 | `rt-consensus-injection` | `prompt.py`에 cross-round consensus 주입("재론 금지"). followup이 누적 합의 참조 | 05 | 멀티라운드 품질 레버 |
| 07 | `rt-convergence` | `convergence.py`(순수): doom-loop(fail 윈도우 3→warn/5→escalate) + findings 중복 감지 → 정지/escalate | 04 | tunaLlama 위임 후보 |
| 08 | `rt-store-dedup` | 4개 구조화 store 공통 base, `_now_iso` dedup, facade private 접근 제거, suppress→로깅 | 01 | 위생 |
| 09 | `rt-transport-parity` | tunaDish rt 구현, Discord 영속화, Telegram 배선 통일 | 02 | 패리티 |

## 5. Verification Strategy

### 정적
- `ruff check src tests` 신규 위반 0, `ty check` src 진단 회귀 0(현 29).
- `tests/test_file_sizes.py` <800 유지; 신규 rt 모듈 ≤250(리뷰 자율).
- `tests/test_layering.py` core→transport 역방향 import 0 유지.

### 동적
- 순수 모듈 단위 테스트(roles/aggregate/consensus/convergence): 결정적, 입출력 케이스.
- 통합: fake runtime으로 `!rt` 다라운드 시나리오(role 분배→synthesizer→consensus 주입→수렴정지).
- 실제 smoke: codex_app 2~3 참가자로 1회 라운드 실행, SynthesisArtifact 채워짐 확인.

### dogfooding
- **tunaDocs**: 본 plan + `plan-task`로 task 문서 분해, 진행 중 doc-reviewer 검토.
- **tunaLlama(glm-5.1:cloud)**: 순수 헬퍼(02/04/05/07) 코드 생성 위임 → 내가 spec 작성·리뷰·배선·테스트. 분해(01)/배선/패리티(09)는 정확성 critical이라 직접.

## 6. Risks & Mitigations

| 위험 | 영향 | 완화 |
|------|------|------|
| 분해(01) 중 control flow 변경 | rt 동작 회귀 | extract-only, 기존 테스트 통과 후에만 기능 추가 |
| role 프롬프트가 출력 포맷 깨뜨림 | synthesis 파싱 실패 | consensus 마커는 md fallback 병행(tunaFlow 패턴) |
| tunaLlama 산출물 품질 | 버그 유입 | 순수 함수만 위임 + 단위 테스트로 검증, 내가 최종 리뷰 |
| 구조화층 활성화가 기존 persist 깨뜨림 | 데이터 손상 | suppress→로깅으로 가시화, 스키마 버전 유지 |
| 비대화 재발 | 유지보수성 저하 | 패키지 분해 선행 + ≤250 자율선 + 순수함수 우선 |

## 7. Out-of-band 영향
- `docs/plans/index.md`에 본 plan 등록.
- 완료 시 README "주요 기능"의 `!rt` 설명 갱신(role/synthesis 추가).
- 메모리 [[tunapi-purpose-and-engine-strategy]]에 rt 고도화 진행 기록.

## 8. 후속 검토 트리거
- tunaFlow rt 알고리즘 추가 진화 시(`rtAlgorithmEnhancementIdeas.md` 갱신) 재검토.
- codex_app 외 엔진 다양성 확보 시(Gemini 3.5/Antigravity) role 분배 정책 재평가.
