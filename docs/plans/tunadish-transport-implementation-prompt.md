# tunadish Transport 구현 프롬프트

> 상태: 실행용 프롬프트
> 작성일: 2026-03-22
> 목적: 클로드가 현재 `tunapi` 저장소에서 바로 Phase 1~4를 구현할 수 있도록, 사실관계와 범위를 고정한 실행 지침을 제공한다.

## 사용 방법

이 문서는 **클로드에게 그대로 전달하는 구현 프롬프트**다.  
핵심은 다음 두 가지다.

- `Phase 1~4`는 지금 이 `tunapi` 레포에서 직접 구현 가능한 범위다.
- `Phase 5(SQLite)`는 지금 당장 `tunapi`에서 하지 말고, `tunadish` 클라이언트와 서버 API 계약이 안정화된 뒤 착수한다.

## 클로드에게 전달할 프롬프트

```md
당신은 `/home/d9ng/privateProject/tunapi` 레포에서 작업하는 구현 에이전트다.

목표는 `tunapi`에 `tunadish` 전용 transport backend를 추가하는 것이다. 이 작업은 저장소 교체나 전면 재설계가 아니라, 이미 존재하는 core 모델을 `tunadish`가 소비할 수 있도록 `transport/API 계층`을 올리는 작업이다.

반드시 아래 사실관계를 전제로 작업하라.

### 검증된 현재 상태

1. `tunadish` 전용 transport backend는 아직 없다.
   - 현재 등록된 transport는 `telegram`, `mattermost`, `slack`뿐이다.
   - 근거: `pyproject.toml`

2. 하지만 `tunadish`가 소비할 core 준비물은 이미 있다.
   - `ProjectMemoryFacade`, `ProjectContextDTO`
   - `HandoffURI`
   - 멀티 transport 실행 기반과 관련 테스트
   - 근거:
     - `src/tunapi/core/memory_facade.py`
     - `src/tunapi/core/handoff.py`
     - `src/tunapi/settings.py`
     - `src/tunapi/cli/run.py`
     - `tests/test_multi_transport.py`

3. 따라서 예전 계획 문서의 Step 1/2/3을 다시 구현하면 안 된다.
   - 이미 있는 DTO 확장, Handoff 필드, 멀티 transport, 관련 테스트를 중복 구현하지 말라.

4. 이번 작업의 중심은 `tunadish transport + WebSocket/JSON-RPC surface` 구현이다.

### 절대 바꾸지 말 것

- 기존 `telegram`, `mattermost`, `slack` transport 동작
- 기존 JSON store의 권위(authority)
- `ProjectMemoryFacade`와 core store의 소유권 구조

### 설계 원칙

1. `tunadish`는 독립 transport id로 추가한다.
2. transport의 1차 책임은 WebSocket + JSON-RPC 서버다.
3. 외부에는 core store를 직접 노출하지 말고, `ProjectMemoryFacade`를 감싼 coarse-grained RPC만 노출한다.
4. 기존 단일 transport 사용 흐름은 깨지면 안 된다.
5. 새 구현은 멀티 transport 병렬 실행 경로와 자연스럽게 호환되어야 한다.
6. 의존성은 가능하면 추가하지 말고, 이미 있는 `websockets`를 우선 사용한다.

### 구현 범위

#### Phase 1. Skeleton

목표:
- `tunapi run --transport tunadish` 또는 동등한 설정으로 backend가 정상 부팅/종료되게 만든다.

필수 산출물:
- `pyproject.toml`에 `tunadish` transport entry point 추가
- `src/tunapi/tunadish/` 패키지 추가
- backend 골격
- 최소 설정 스키마
- 기존 transport backend 패턴을 따르는 lifecycle 연결

완료 기준:
- 단독 실행 가능
- 기존 transport와 병행 실행 가능
- 설정 오류 시 기존 스타일과 일관된 에러 처리

#### Phase 2. Read-Only RPC

목표:
- `tunadish` 클라이언트가 프로젝트 상태를 읽고 handoff를 처리할 수 있는 최소 JSON-RPC surface를 제공한다.

우선 구현할 메서드:
- `project.list`
- `project.get_context`
- `project.get_context_markdown`
- `handoff.create`
- `handoff.parse`
- `engine.list`
- `session.open`

원칙:
- `ProjectMemoryFacade`를 그대로 RPC에 1:1 노출하지 말고, UI 친화적인 요청/응답 형태로 감싼다.
- 응답 구조는 coarse-grained 하게 유지한다.
- API contract는 문서화 가능한 형태로 정리한다.

완료 기준:
- 최소한 프로토콜 테스트 또는 transport 레벨 테스트로 주요 메서드가 검증된다.
- handoff round-trip이 RPC 경계에서도 성립한다.

#### Phase 3. Run Integration

목표:
- `tunadish`가 읽기 전용 뷰어가 아니라 실제 `tunapi` 실행 세션을 열고 재개할 수 있게 만든다.

우선 구현할 메서드:
- `run.execute`
- `run.cancel`
- `session.resume`

추가 요구:
- 실행 진행 상태를 WebSocket notification으로 보낼 것
- `handoff/open/resume` 흐름이 이어질 것
- transport backend가 실제 runtime과 연결될 것

완료 기준:
- 최소 1개의 실행 경로가 `tunadish` transport에서 end-to-end로 동작한다.
- 취소와 재개 동작이 테스트 또는 검증 가능한 방식으로 보호된다.

#### Phase 4. Write API + Events

목표:
- 프로젝트 메모리 쓰기와 상태 변경 알림을 `tunadish`에서 사용할 수 있게 만든다.

우선 구현할 메서드:
- `memory.add`
- `discussion.save_roundtable`
- `discussion.link_branch`
- `synthesis.create_from_discussion`
- `review.request`

이벤트 범위:
- 상태 변경 notification
- 필요한 경우 최소 구독/브로드캐스트 관리자

완료 기준:
- 읽기 API만이 아니라 핵심 쓰기 흐름까지 `tunadish` transport를 통해 수행 가능
- 최소한 메모리/토론/리뷰 경로 중 핵심 회귀가 자동 테스트로 보호됨

### 구현 순서상 주의

- 한 번에 다 하지 말고 Phase 단위로 끊어서 구현하라.
- 각 Phase 끝에서 테스트를 실행하고, 남은 리스크를 기록하라.
- 기존 설계 문서가 현재 코드와 충돌하면 코드를 우선 사실로 취급하라.
- 새 추상화는 최소화하고 기존 backend 패턴을 최대한 재사용하라.

### 범위 밖

이번 작업에서 하지 말 것:
- `tunapi`의 JSON store를 SQLite로 교체
- `tunadish` 클라이언트 구현
- 클라이언트 로컬 캐시/검색 DB 도입
- 실시간 멀티디바이스 동기화의 완전한 해결

### 검증

가능한 범위에서 아래를 수행하라.

1. 관련 단위 테스트 추가/갱신
2. 최소 회귀 테스트 실행
3. 가능하면 `tunadish` 단독 부팅 smoke test
4. 가능하면 기존 transport + `tunadish` 멀티 transport smoke test

최종 보고는 다음 형식으로 하라.

1. 구현한 Phase
2. 변경한 핵심 파일
3. 테스트 결과
4. 남은 리스크
5. 다음 Phase 착수 조건
```

## Phase 5를 지금 하지 말아야 하는 이유

`Phase 5`는 `tunadish` 클라이언트 쪽 SQLite 도입이다.  
이 단계는 지금 `tunapi` 레포의 직접 과제가 아니다.

지금 SQLite를 먼저 설계하면 아래가 아직 고정되지 않아 다시 뜯어고칠 가능성이 크다.

- `tunadish`가 실제로 소비하는 RPC contract
- 서버 권위 데이터와 클라이언트 로컬 캐시의 경계
- 이벤트 스트림의 shape
- `session.open`, `session.resume`, `run.execute` 이후 로컬에 무엇을 저장해야 하는지

즉 순서는 `SQLite 먼저`가 아니라 `transport/API 먼저`가 맞다.

## Phase 5 착수 시점

아래 조건을 모두 만족하면 그때 `tunadish`에서 SQLite를 도입한다.

1. `Phase 1~4`가 끝나서 `tunadish` transport가 실제로 부팅되고 읽기/실행/쓰기 경로를 제공한다.
2. 최소 1개의 실제 또는 테스트용 `tunadish` 클라이언트가 이 RPC를 소비해본 상태다.
3. RPC method set과 주요 payload shape가 단기적으로 크게 바뀌지 않을 만큼 안정화됐다.
4. "왜 로컬 DB가 필요한가"가 구체적으로 확인됐다.
   - 예: 빠른 검색
   - 예: 오프라인 재열기
   - 예: 최근 세션 복원
   - 예: UI 상태 보존
   - 예: 이벤트 재생 없이 초기 화면 즉시 렌더링

## Phase 5에서 해야 할 일

이 시점의 `SQLite`는 서버 권위를 옮기는 작업이 아니라, **`tunadish` 클라이언트의 로컬 저장소**를 만드는 작업이어야 한다.

우선 작업은 아래 순서를 따른다.

1. 권위 경계 확정
   - `tunapi`가 권위 데이터로 계속 유지할 것
   - `tunadish` SQLite는 우선 캐시, 검색 인덱스, UI 상태, 로컬 draft 중심으로 둘 것

2. 동기화 모델 확정
   - 초기 스냅샷을 어떤 RPC로 채울지
   - 이후 변경을 어떤 이벤트로 반영할지
   - 재연결 시 어떤 cursor 또는 재동기화 규칙을 쓸지

3. 최소 스키마 설계
   - `projects`
   - `project_context_cache`
   - `sessions`
   - `messages` 또는 동등한 대화 로그 테이블
   - `handoff_history`
   - `ui_state`
   - `sync_state`
   - 필요 시 FTS5 테이블

4. 마이그레이션/복구 전략
   - 앱 버전 업 시 schema migration
   - 캐시 손상 시 재구축
   - 서버 truth 기준 재동기화

5. 검색과 성능 최적화
   - 실제 메시지량과 검색 UX가 나온 뒤 FTS5 범위를 확정
   - 너무 이른 정규화/최적화는 피할 것

## 한 줄 결론

지금 클로드가 직접 구현해야 하는 것은 `tunapi`의 `tunadish transport/API 계층`이다.  
`SQLite`는 그 계층이 실제로 돌아가고, `tunadish` 클라이언트가 어떤 데이터를 로컬에 들고 있어야 하는지 확인된 뒤에 시작한다.

