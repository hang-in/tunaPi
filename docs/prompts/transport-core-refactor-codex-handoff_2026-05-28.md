---
title: Transport Core 통합 및 비대 파일 분해 — Codex Handoff
type: prompt
status: in_progress
priority: P1
updated_at: 2026-05-28
owner: codex-main
target_agent: codex-cli
paired_plan: docs/plans/transport-core-consolidation-and-fatfile-decomposition.md
expected_output: Task 02부터 이어서 코드 diff, 작은 논리 커밋, 검증 로그, 다음 작업 포인트를 남긴다.
---

# Transport Core 통합 및 비대 파일 분해 — Codex Handoff

## 1. Context (Architect → Developer 인계)

현재 목표는 `docs/plans/transport-core-consolidation-and-fatfile-decomposition.md`의 Task 02부터 이어서 진행하는 것이다. 최종 목표는 Mattermost/Slack `loop.py`의 쌍둥이 헬퍼를 `core/`로 추출하고, 이후 명령 핸들러 통합과 Telegram/Discord/Tunadish 분해로 넘어가는 것이다.

작업 환경은 사용자가 집에서 `~/privateProject/tunapi`에서 Codex를 다시 실행할 수 있는 상태다. 현재 브랜치는 `main`이고 원격 대비 여러 커밋이 ahead 상태다. 이 문서는 이어받는 Codex가 이전 대화 없이도 바로 현재 상태를 복구하고 다음 작업을 판단할 수 있도록 작성했다.

주의할 점:

- 사용자는 “기능 안정화, 고도화, 새 기능 추가를 잘 할 수 있는 기준”으로 알아서 리팩토링을 진행하라고 했다.
- 코드를 보지 않는 사용자이므로, 진행 후에는 “무엇을 왜 했는지”만 보고하면 된다.
- 한 번에 큰 추출을 하지 말고, 테스트 가능한 작은 커밋으로 쪼갠다.
- `scripts/slack_cleanup.py`는 로컬 ignored 상태이며 민감 Slack 토큰이 있었으므로 절대 커밋하지 않는다. 토큰은 폐기/재발급 대상이다.

## 2. 현재 Git 상태

마지막 확인 기준:

```bash
git status --short --branch
# ## main...origin/main [ahead 9]
#  M src/tunapi/core/chat_loop_helpers.py
#  M src/tunapi/mattermost/loop.py
#  M src/tunapi/slack/loop.py
```

최근 커밋:

```text
7d10b37 refactor: share chat file result formatting
882be4d refactor: share cancel reaction handling
12fdf4b refactor: extract shared chat loop helpers
9e31534 test: add refactor guardrails
e63aaf9 test: update progress render expectation
135fead feat: improve progress and roundtable runtime
69ea99f fix: harden runtime state and diagnostics
794ae0c docs: add refactor plan and review records
00442aa docs: add operational resilience & observability hardening plan
```

이미 완료된 정리:

- `debug.log` 변경은 복원해서 제거했다.
- `.omc/`, `.pi/`, `scripts/slack_cleanup.py`는 `.git/info/exclude`에 넣어 로컬 산출물로 숨겼다.
- Task 01 guardrail은 커밋 완료: `tests/test_layering.py`, `tests/test_file_sizes.py`.

## 3. 이미 완료된 작업

### Task 01 — dep-graph guardrails

커밋: `9e31534 test: add refactor guardrails`

추가 파일:

- `tests/test_layering.py`: `src/tunapi/core/`가 `tunapi.discord`, `tunapi.mattermost`, `tunapi.slack`, `tunapi.telegram`, `tunapi.tunadish`를 import하지 못하게 AST로 검사한다.
- `tests/test_file_sizes.py`: 현재 800 LoC 초과 Python 파일 11개를 baseline으로 고정한다. 새 oversized 파일 유입을 막는다.

검증 완료:

```bash
uv run pytest tests/test_layering.py tests/test_file_sizes.py --no-cov -q
# 2 passed
uv run ruff check tests/test_layering.py tests/test_file_sizes.py
# All checks passed
uv run ruff format --check tests/test_layering.py tests/test_file_sizes.py
# 2 files already formatted
```

### Task 02 — MM/Slack loop helper extraction, 1차

커밋: `12fdf4b refactor: extract shared chat loop helpers`

추출된 core 파일:

- `src/tunapi/core/chat_loop_helpers.py`

이 커밋에서 core로 옮긴 흐름:

- upload dir resolution
- send-to-channel wrapper
- persona prefix resolution
- roundtable thread start 공통 흐름

Transport 파일에는 기존 테스트/patch 경로 보존을 위해 얇은 wrapper를 남겼다.

### Task 02 — cancel reaction handling

커밋: `882be4d refactor: share cancel reaction handling`

추출 내용:

- `handle_cancel_reaction_by_message_id(...)`를 `core/chat_loop_helpers.py`에 추가.
- Mattermost는 `emoji_name/post_id`, Slack은 `emoji/item_ts`를 wrapper에서 꺼내 core helper로 넘긴다.

검증 완료:

```bash
uv run pytest tests/ -k "slack or mattermost or mm" --no-cov -q
# 1169 passed, 2371 deselected
```

### Task 02 — file result formatting

커밋: `7d10b37 refactor: share chat file result formatting`

추출 내용:

- `render_file_put_results(results)`
- `render_saved_file_context(results)`

Mattermost/Slack `_resolve_prompt`와 `/file put` 결과 메시지에서 동일한 포맷 생성을 core helper로 옮겼다.

검증 완료:

```bash
uv run ruff check src/tunapi/core/chat_loop_helpers.py src/tunapi/mattermost/loop.py src/tunapi/slack/loop.py
# All checks passed
uv run pytest tests/ -k "slack or mattermost or mm" --no-cov -q
# 1169 passed, 2371 deselected
```

## 4. 현재 미완료 WIP 상태

현재 uncommitted 변경은 roundtable archive 공통화 중간 상태다.

변경 파일:

- `src/tunapi/core/chat_loop_helpers.py`
- `src/tunapi/mattermost/loop.py`
- `src/tunapi/slack/loop.py`

현재 diff stat:

```text
src/tunapi/core/chat_loop_helpers.py | 48 ++++++++++++++++++++++++++++++++-
src/tunapi/mattermost/loop.py        | 52 +++++++-----------------------------
src/tunapi/slack/loop.py             | 46 +++++++------------------------
3 files changed, 67 insertions(+), 79 deletions(-)
```

의도:

- `_archive_roundtable(...)` 본문을 `archive_roundtable_thread(...)`로 `core/chat_loop_helpers.py`에 추출했다.
- Mattermost/Slack wrapper는 `close_message`만 다르게 넘긴다.
- Mattermost 종료 문구: `🔴 라운드테이블이 종료되었습니다.`
- Slack 종료 문구: `Roundtable closed.`

현재 검증 상태:

```bash
uv run pytest tests/ -k "slack or mattermost or mm" --no-cov -q
# 1169 passed, 2371 deselected
```

하지만 ruff는 실패한다. 이유는 archive 본문 제거 후 두 transport loop에 `JournalEntry` import가 남아 있기 때문이다.

```text
F401 `..journal.JournalEntry` imported but unused
src/tunapi/mattermost/loop.py:28
src/tunapi/slack/loop.py:34
```

집에서 이어받으면 첫 작업은 다음이다.

```bash
# 1. 두 파일에서 JournalEntry import 제거
# src/tunapi/mattermost/loop.py
# src/tunapi/slack/loop.py

uv run ruff format src/tunapi/core/chat_loop_helpers.py src/tunapi/mattermost/loop.py src/tunapi/slack/loop.py
uv run ruff check src/tunapi/core/chat_loop_helpers.py src/tunapi/mattermost/loop.py src/tunapi/slack/loop.py
uv run pytest tests/ -k "slack or mattermost or mm" --no-cov -q

git add src/tunapi/core/chat_loop_helpers.py src/tunapi/mattermost/loop.py src/tunapi/slack/loop.py
git commit -m "refactor: share roundtable archive handling"
```

## 5. 작업 범위

이 핸드오프의 범위는 paired plan의 Task 02를 계속 진행하는 것이다.

Task 02 원문 요지:

- MM/Slack `loop.py` 쌍둥이 헬퍼를 `core/chat_loop_helpers.py`로 추출한다.
- 후보: `_dispatch_rt_command`, `_resolve_persona_prefix`, `_auto_bind_channel_project`, `_send_to_channel`, `_handle_cancel_reaction`, `_handle_voice`, `_handle_file_command`, `_resolve_prompt`, `_run_engine`.

현재까지 처리됨:

- `_resolve_upload_dir` wrapper화
- `_send_to_channel` wrapper화
- `_resolve_persona_prefix` wrapper화
- `_start_roundtable` 공통화
- `_handle_cancel_reaction` 공통화
- file result/context formatting 공통화
- `_archive_roundtable` 공통화 WIP

아직 남은 주요 후보:

- `_dispatch_rt_command`: 거의 동일하다. `thread_id = msg.root_id` vs `msg.thread_ts` 차이만 wrapper에서 넘기면 core화 가능하다.
- `_auto_bind_channel_project`: 거의 동일하지만 bot channel API 반환 타입이 다를 수 있어 테스트 확인 후 진행한다.
- `_resolve_prompt`: 파일 모델과 trigger mention 문법 차이가 있어 한 번에 추출하지 말고 “후반부 prompt finalization”만 별도 helper로 추출하는 방식을 고려한다.
- `_handle_file_command`: upload/download API 차이가 있어 공통 formatter 이상으로 무리하지 않는다.
- `_run_engine`: 큰 중복이지만 runner/context/thread handling이 섞여 있어 Task 02 후반부 또는 별도 커밋으로 진행한다.

## 6. 우선순위 / 순서

1. 현재 WIP를 마무리한다.

- `JournalEntry` unused import 제거.
- ruff/test 통과 확인.
- `refactor: share roundtable archive handling` 커밋.

2. `_dispatch_rt_command` 공통화를 시도한다.

- core helper 이름 후보: `dispatch_roundtable_command(...)` 또는 `build_roundtable_callbacks(...)`.
- transport wrapper에서 넘겨야 할 값:
  - `thread_id`: MM `msg.root_id`, Slack `msg.thread_ts`
  - `channel_id`: 둘 다 `msg.channel_id`
  - `start_roundtable` callback: 각 transport의 `_start_roundtable(...)`
  - `handle_rt`: 지금은 각 transport commands에서 import한 함수지만 이름/계약이 동일하다.
- 완료 후 반드시 `tests/test_mm_loop_dispatch.py`, `tests/test_slack_loop_dispatch.py`, `tests/test_mm_loop.py`, `tests/test_slack_loop.py`를 돌린다.

3. `_auto_bind_channel_project`를 확인한다.

- 공통 흐름은 projects_root 확인 → 기존 binding 확인 → bot.get_channel → channel.name 매칭 → register_discovered.
- Slack/MM channel 객체 타입 차이를 `get_channel_name` callback으로 감싸면 core화 가능하다.
- 단, 이 함수는 실제 runtime private `_projects`를 건드리므로 테스트가 있는지 먼저 확인한다.

4. `_resolve_prompt`는 보수적으로 접근한다.

- 이미 file result/context formatting만 core화했다.
- 다음으로 가능한 최소 추출은 `finalize_prompt_text(...)` 정도다.
- transport 차이:
  - Mattermost: `bot_username`, `@name`, direct message 처리
  - Slack: `bot_user_id`, `<@id>`, thread reply always trigger
- `should_trigger`와 `strip_mention` 자체는 transport별로 남기고, 호출 순서만 core helper로 빼는 정도가 안전하다.

5. `_run_engine`은 마지막에 다룬다.

- LoC 절감 효과는 크지만 context resolution, engine override, chat session resume, progress send가 얽혀 있다.
- 먼저 dispatch/auto-bind/prompt를 끝내고 테스트 기반이 안정된 뒤 진행한다.

## 7. 핵심 INV (위반 금지)

- 외부 사용자 동작 변경 금지. 메시지 문구/명령 응답/roundtable 흐름은 바뀌면 안 된다.
- `core/`에서 transport 모듈 import 금지. `tests/test_layering.py`가 이걸 막는다.
- `core`에 `if transport == "slack"` 같은 분기 추가 금지. 차이는 callback/value injection으로 넘긴다.
- 기존 private import/patch 경로가 테스트에 쓰이면 wrapper나 compatibility export를 남긴다.
- 한 커밋은 한 관심사만 포함한다. 이미 이 방식으로 진행 중이다.
- `scripts/slack_cleanup.py`, `.omc/`, `.pi/`, `debug.log` 등 로컬 산출물 커밋 금지.
- 실패한 검증을 숨기지 말고, 실패 상태면 먼저 고친 뒤 커밋한다.

## 8. Changed files (전체)

이미 커밋된 주요 파일:

- `tests/test_layering.py`
- `tests/test_file_sizes.py`
- `src/tunapi/core/chat_loop_helpers.py`
- `src/tunapi/mattermost/loop.py`
- `src/tunapi/slack/loop.py`
- `tests/test_exec_render.py`

현재 WIP 파일:

- `src/tunapi/core/chat_loop_helpers.py`
- `src/tunapi/mattermost/loop.py`
- `src/tunapi/slack/loop.py`

다음 단계에서 변경 가능성이 높은 파일:

- `src/tunapi/core/chat_loop_helpers.py`
- `src/tunapi/mattermost/loop.py`
- `src/tunapi/slack/loop.py`
- `tests/test_mm_loop.py`
- `tests/test_slack_loop.py`
- `tests/test_mm_loop_dispatch.py`
- `tests/test_slack_loop_dispatch.py`

## 9. Verification (전체)

작업 재개 직후 현재 WIP 검증:

```bash
uv run ruff format src/tunapi/core/chat_loop_helpers.py src/tunapi/mattermost/loop.py src/tunapi/slack/loop.py
uv run ruff check src/tunapi/core/chat_loop_helpers.py src/tunapi/mattermost/loop.py src/tunapi/slack/loop.py
uv run pytest tests/ -k "slack or mattermost or mm" --no-cov -q
```

Task 02 각 커밋 전 최소 검증:

```bash
uv run ruff check src/tunapi/core/chat_loop_helpers.py src/tunapi/mattermost/loop.py src/tunapi/slack/loop.py
uv run ruff format --check src/tunapi/core/chat_loop_helpers.py src/tunapi/mattermost/loop.py src/tunapi/slack/loop.py
uv run pytest tests/test_mm_loop.py tests/test_slack_loop.py tests/test_mm_loop_dispatch.py tests/test_slack_loop_dispatch.py tests/test_layering.py tests/test_file_sizes.py --no-cov -q
```

Task 02 큰 단위 완료 후 권장 검증:

```bash
uv run pytest tests/ -k "slack or mattermost or mm" --no-cov -q
uv run pytest tests/test_layering.py tests/test_file_sizes.py --no-cov -q
uv run ruff check src tests
uv run ruff format --check src tests
```

마지막 PR/푸시 전 전체 검증:

```bash
just check
```

참고: `uv`가 sandbox 안에서 `~/.cache/uv` 접근 오류를 낼 수 있다. Codex에서 권한 요청이 뜨면 승인해서 실행하면 된다.

## 10. 리뷰 의뢰 시 첨부할 내용

Reviewer에게 넘길 때 포함할 것:

- Task 02에서 core로 옮긴 helper 목록.
- MM/Slack loop LoC 변화.
- `core`가 transport를 import하지 않는다는 `tests/test_layering.py` 결과.
- MM/Slack 관련 테스트 결과.
- 의도적으로 남긴 compatibility shim:
  - `tunapi.mattermost.loop._resolve_persona_prefix`
  - `tunapi.slack.loop._resolve_persona_prefix`
  - `_PERSONA_PREFIX_RE` re-export (`# noqa: F401`)는 기존 테스트/import 호환 목적이다.

## 11. 위임 정책

`tunaLlama` 스킬은 확인했지만 현재 이 세션에서는 `tuna_*` MCP 도구가 노출되지 않았다. 집 환경에서 도구가 노출되면 작은 분석/테스트 작성에는 위임 가능하다. 단, architecture 판단과 helper boundary 결정은 Codex가 직접 유지한다.

`context-mode`는 큰 diff/파일 분석에 유용하다. 큰 파일을 그대로 읽기보다 다음처럼 요약/구조 분석에 사용한다.

```bash
# 예: 함수 위치/라인 수만 뽑기
# ctx_execute python으로 ast 파싱 후 함수명/line range 출력
```

## 12. 이어받는 Codex에게 바로 줄 지시문

```text
~/privateProject/tunapi에서 이어서 작업해줘.
먼저 docs/prompts/transport-core-refactor-codex-handoff_2026-05-28.md를 읽고, 현재 WIP인 roundtable archive 공통화부터 마무리해.
JournalEntry unused import 제거 → ruff/test 통과 → commit: "refactor: share roundtable archive handling".
그 다음 Task 02의 다음 후보인 _dispatch_rt_command 공통화를 작은 커밋으로 진행해.
core에서 transport import 금지, 기존 테스트/patch 경로 호환 유지, 커밋 전 MM/Slack 관련 테스트와 layering/file-size guardrail을 실행해.
```
