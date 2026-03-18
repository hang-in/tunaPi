# Roundtable Plan

채널에서 여러 AI 에이전트에게 역할을 부여하고 토론시키는 기능.
핵심 원칙: **모바일에서 엄지 하나로 모든 조작 가능** — toml 수정 최소화, 채팅 명령어 중심.

## 현재 상태 (이미 있는 것)

- `chat_prefs.py`: `get_context`/`set_context` 존재하지만 **미사용** — 배선만 하면 프로젝트 바인딩 가능
- `commands.py`: `/help`, `/model`, `/trigger` 등 match 기반 하드코딩 — 확장 필요
- `parsing.py`: 자기 메시지만 필터링, 다른 봇 메시지는 통과 — 루프 방지 필요
- `settings.py`: `projects` dict + `chat_id` 매핑 지원, `watch_config` 플래그 존재 (MM에서 미연결)
- `bridge.py`: `props` 전달 가능하나 Interactive Button 콜백 미구현
- `runner.py`: `get_run_base_dir()`로 cwd 설정, context → worktree 자동 생성 지원

## 명령어 접두어

Mattermost 모바일은 미등록 slash command를 차단하므로 `!` 접두어 사용.
설정 가능하게: `command_prefix = "!"` (tunapi.toml)

## Phase 1: 채널-프로젝트 바인딩

**목표:** 채팅에서 프로젝트를 선택하고, 에이전트가 해당 디렉토리에서 실행되게 연결

### 변경 파일
- `settings.py`: `projects_root: str | None` 필드 추가
- `chat_prefs.py`: 이미 있는 `set_context`/`get_context` 활용
- `commands.py`: `!project` 명령 추가
- `loop.py`: `ambient_context=None` → `chat_prefs.get_context()` 연결

### 명령어
```
!project list              # projects_root 스캔 + 등록된 프로젝트 목록
!project set <name>        # 현재 채널에 프로젝트 바인딩
!project info              # 현재 채널의 프로젝트 정보
```

### 구현 포인트
- `!project list` 실행 시 `projects_root` 하위 `.git` 디렉토리 스캔 (lazy, 재시작 불필요)
- 선택된 프로젝트를 `chat_prefs.set_context(channel_id, RunContext(project=name))` 저장
- `loop.py`의 context 해석에서 `chat_prefs.get_context()` 값을 우선 사용

## Phase 2: 명령어 디스패치 일반화

**목표:** 하드코딩된 match 블록을 확장 가능한 구조로 리팩터링

### 변경 파일
- `commands.py`: command registry 패턴 도입 (Telegram `dispatch.py` 참고)
- `loop.py`: 디스패치 로직을 registry에 위임

### 설계
```python
COMMANDS: dict[str, CommandHandler] = {
    "project": handle_project,
    "persona": handle_persona,
    "roundtable": handle_roundtable,
    ...
}
```

### 접두어 처리
- `parse_slash_command()` → `parse_command(text, prefix="!")` 로 확장
- `!`과 `/` 모두 지원 (PC 호환)

## Phase 3: 페르소나 시스템

**목표:** 에이전트에게 역할을 부여하여 토론의 질을 높임

### 변경 파일
- `chat_prefs.py`: 페르소나 저장 (채널별 또는 글로벌)
- `commands.py`: `!persona` 명령 추가
- `runner.py`: 에이전트 실행 시 페르소나 prompt를 사용자 메시지 앞에 prepend

### 명령어
```
!persona add reviewer "시니어 코드 리뷰어. 보안과 성능 관점에서 비판적으로 검토"
!persona add architect "소프트웨어 아키텍트. 확장성과 설계 관점"
!persona list
!persona remove <name>
```

### 저장 구조
```json
{
  "personas": {
    "reviewer": {"name": "리뷰어", "prompt": "..."},
    "architect": {"name": "아키텍트", "prompt": "..."}
  }
}
```

### 구현 포인트
- 페르소나는 글로벌 저장 (채널이 아니라 사용자 레벨)
- Runner에 `system_prefix: str | None` 파라미터 추가
- prepend 방식: `[역할: 리뷰어]\n{persona.prompt}\n\n---\n\n{user_message}`

## Phase 4: Roundtable (토론 기능)

**목표:** 같은 주제에 대해 여러 에이전트의 의견을 수집

### v0: 순차 의견 수집 (사람 중재)
가장 안전한 최소 구현. 자동 체이닝 없음.

```
!roundtable start "이 PR 설계 어떤가?" claude=reviewer gemini=architect codex=coder
```

동작:
1. 스레드 생성
2. 각 에이전트를 순차 실행 (페르소나 prompt 포함)
3. 응답을 같은 스레드에 순서대로 게시
4. 사용자가 추가 질문하면 다시 전체 에이전트 순회 또는 특정 에이전트만 호출

### 변경 파일
- `commands.py`: `!roundtable` 명령 추가
- `roundtable.py` (신규): 라운드테이블 세션 관리
- `loop.py`: 라운드테이블 스레드 내 메시지 감지 및 라우팅
- `parsing.py`: 봇 메시지 필터링 강화 (다른 봇 ID 목록 관리)

### 안전장치
- `max_agents`: 2~3 (기본값 3)
- `max_rounds`: 라운드 수 제한 (기본값 1, v0)
- 토론 중 에이전트는 read-only (파일 수정 불가)
- 봇 간 루프 방지: `parsing.py`에 known bot ID 필터 추가

### v1 (이후): 자동 리버틀
- `!roundtable start --rounds 3` — 에이전트 A 응답을 B 입력에 포함
- 라운드 카운터 + 비용 캡으로 제어
- 파일 수정 필요 시 에이전트별 worktree 분리 (`debate/<topic>/<agent>` 브랜치)

### 세션 구조
```python
@dataclass
class RoundtableSession:
    thread_id: str
    topic: str
    participants: list[RoundtableParticipant]  # (engine, persona_name)
    current_round: int
    max_rounds: int
    read_only: bool
    transcript: list[RoundtableEntry]  # (agent, response) 기록
```

## 실행 순서 요약

| 순서 | 작업 | 난이도 | 의존성 |
|------|------|--------|--------|
| 1 | `!` 접두어 지원 | 낮음 | 없음 |
| 2 | `!project` 명령 + context 배선 | 낮음 | Phase 1 |
| 3 | 명령어 디스패치 일반화 | 중간 | Phase 2 |
| 4 | `!persona` 명령 + prompt prepend | 중간 | Phase 3 |
| 5 | `!roundtable` v0 (순차 의견 수집) | 높음 | Phase 2, 3, 4 |
| 6 | 봇 간 루프 방지 (parsing) | 중간 | Phase 5 전에 |
| 7 | `!roundtable` v1 (자동 리버틀) | 높음 | Phase 5 안정화 후 |

## 비고

- 버튼 UI: Mattermost Interactive Button 콜백 처리가 없으므로 v1 이후로 미룸
- 비용 추적: JSONL 이벤트 스트림에 usage 포함 시에만 수집 (로그 파싱 비추)
- config watcher: Mattermost에도 Telegram처럼 연결 필요 (별도 이슈)
