---
title: Transport Core 통합 및 비대 파일 분해 리팩토링 — Gemini Handoff
type: prompt
status: ready
priority: P1
updated_at: 2026-05-28
owner: codex
target_agent: gemini
paired_plan: docs/plans/transport-core-consolidation-and-fatfile-decomposition.md
expected_output: 남은 Discord/Tunadish 비대 파일 분해 diff + 변경 파일 기준 lint/관련 테스트/전체 pytest 통과 결과
---

# Transport Core Refactor — Gemini Handoff

## 1. Context (Codex → Gemini 인계)

사용자가 "리팩토링 끝까지" 진행 요청. Codex가 2026-05-28에 Gemini WIP를 확인하고 이어서 Phase B 대부분과 Phase C 일부를 커밋 완료했다.

현재 브랜치 상태:
- `main...origin/main [ahead 32]`
- 작업트리에는 테스트 부산물 `debug.log`만 unstaged로 남아 있음. 커밋하지 말 것.
- 최근 전체 검증: `uv run pytest tests/ --no-cov -q` → `3542 passed, 3 warnings`
- 반복 경고 3개는 기존 경고로 보임:
  - `tests/test_coverage_push.py::TestUploadContent::test_upload`
  - `tests/test_logging_extra.py::test_safe_writer_isatty_missing`
  - `tests/test_logging_extra.py::test_safe_writer_value_error_flush`

중요: `uv run ruff check src tests` 전체는 기존 unrelated lint 위반이 많아 실패한다. 이번 리팩토링에서는 변경 파일 기준 lint를 통과시키고, 전체 pytest를 기준선으로 삼았다.

## 2. 작업 범위

paired plan 기준 완료/진행 상태:
- Task 03 `mm-slack-tunadish-commands-unify`: 완료. 공통 chat command handler 분리됨.
- Task 04 `telegram-state-migrate-to-core`: 완료. Telegram chat prefs/sessions/outbox core 이동.
- Task 05 `discord-state-migrate-to-core`: 완료. Discord outbox core 재사용.
- Task 06 `discord-handle-message-split`: 부분 완료.
  - `src/tunapi/discord/handlers.py`는 777 LoC로 800줄 아래 달성.
  - `src/tunapi/discord/loop.py`는 아직 1780 LoC.
- Task 07 `tunadish-backend-split`: 부분 시작.
  - rawq 관련 로직만 `src/tunapi/tunadish/rawq_handlers.py`로 분리.
  - `src/tunapi/tunadish/backend.py`는 아직 1711 LoC.

Gemini가 이어서 할 우선 범위:
1. `src/tunapi/discord/loop.py`의 `handle_message` 주변 helper 분리
2. `src/tunapi/tunadish/backend.py`의 branch/message/write API handler 분리
3. 각 분리 후 관련 테스트와 전체 pytest 유지

## 3. 우선순위 / 순서

1. 먼저 현재 상태를 확인:

```bash
git status --short --branch
git log --oneline -12
wc -l src/tunapi/discord/handlers.py src/tunapi/discord/loop.py src/tunapi/tunadish/backend.py
```

2. `discord/loop.py`부터 진행 권장.
   - 이미 분리된 모듈:
     - `src/tunapi/discord/message_utils.py`
     - `src/tunapi/discord/ctx_commands.py`
     - `src/tunapi/discord/engine_commands.py`
     - `src/tunapi/discord/file_commands.py`
     - `src/tunapi/discord/voice_commands.py`
     - `src/tunapi/discord/roundtable_commands.py`
     - `src/tunapi/discord/roundtable_loop.py`
     - `src/tunapi/discord/resume_queue.py`
   - 다음 후보:
     - media group / auto-put attachment flow
     - trigger/context resolution helpers
     - thread creation / branch override helpers
     - voice message transcription helper
   - 테스트들이 `tunapi.discord.loop._start_roundtable`, `_send_queued_progress`, `send_with_resume`, `ResumeResolver`를 직접 import/monkeypatch한다. 기존 symbol 경로는 wrapper로 유지할 것.

3. 그 다음 `tunadish/backend.py`.
   - 이미 분리된 모듈:
     - `src/tunapi/tunadish/rawq_handlers.py`
   - 다음 후보:
     - branch handlers: `_handle_branch_*`, `_build_*branch*`
     - message handlers: `_handle_message_*`
     - write API / handoff handlers: `_handle_discussion_*`, `_handle_synthesis_create`, `_handle_review_request`, `_handle_handoff_*`
     - JSON-RPC dispatch table
   - 테스트들이 backend private method를 직접 호출한다. backend method 이름은 유지하고 내부를 새 모듈 함수로 위임하는 wrapper 방식을 추천.

## 4. 핵심 INV (위반 금지)

- 외부 동작/응답 문구 변경 금지. 리팩토링은 단순 이동/추출 중심.
- `debug.log` 커밋 금지.
- `pyproject.toml` coverage threshold 낮추지 말 것. 현재 `--cov-fail-under=81` 유지.
- 새 800줄 초과 파일 만들지 말 것.
- `core/`에서 transport import 금지.
- `discord.loop`, `tunadish.backend`의 기존 테스트 import/monkeypatch 경로는 가능하면 wrapper로 보존.
- `ruff format src/tunapi/tunadish/backend.py`처럼 큰 파일 전체 포맷 churn 금지. 필요한 파일/신규 파일만 포맷.

## 5. Changed files (현재 Codex가 만든 주요 변경)

최근 커밋:
- `57b2da9 refactor: split tunadish rawq handlers`
- `80417b8 refactor: split discord resume queue helpers`
- `0ec31e6 refactor: split discord roundtable loop helpers`
- `413074d refactor: split discord voice and roundtable commands`
- `83a1a3e refactor: split discord file command`
- `38e0232 refactor: split discord engine commands`
- `e2af199 refactor: split discord context commands`
- `7087052 refactor: split discord message utilities`
- `d48c84f fix: edit final progress summary in bridge`
- `31f9031 refactor: reuse core outbox in discord`
- `b86bf87 refactor: migrate telegram chat state to core`
- `b121621 refactor: share chat command handlers`

주요 신규 파일:
- `src/tunapi/core/chat_command_engine.py`
- `src/tunapi/core/chat_command_help.py`
- `src/tunapi/core/chat_command_memory.py`
- `src/tunapi/core/chat_command_project.py`
- `src/tunapi/discord/ctx_commands.py`
- `src/tunapi/discord/engine_commands.py`
- `src/tunapi/discord/file_commands.py`
- `src/tunapi/discord/message_utils.py`
- `src/tunapi/discord/resume_queue.py`
- `src/tunapi/discord/roundtable_commands.py`
- `src/tunapi/discord/roundtable_loop.py`
- `src/tunapi/discord/voice_commands.py`
- `src/tunapi/tunadish/rawq_handlers.py`

현재 큰 파일:
- `src/tunapi/discord/handlers.py`: 777 LoC
- `src/tunapi/discord/loop.py`: 1780 LoC
- `src/tunapi/tunadish/backend.py`: 1711 LoC
- `src/tunapi/tunadish/rawq_handlers.py`: 236 LoC

## 6. Verification (전체)

작업 전/후 권장 검증:

```bash
# 변경 파일 기준 lint
uv run ruff check <changed-files>

# Discord loop 작업 후
uv run pytest tests/test_discord_loop_helpers.py tests/test_discord_loop_dispatch.py tests/test_rt_telegram_discord.py --no-cov -q

# Tunadish backend 작업 후
uv run pytest tests/test_tunadish_backend.py tests/test_tunadish_backend_extra.py tests/test_tunadish_phase4.py --no-cov -q

# 최종
uv run pytest tests/ --no-cov -q
```

주의:
- `uv run ruff check src tests` 전체는 현재 기존 lint 위반 때문에 실패한다. 변경 파일 기준으로 확인할 것.
- `debug.log`는 테스트 실행 때 계속 수정될 수 있다. 커밋 제외.

## 7. 리뷰 의뢰

Gemini가 작업을 마치면 다음을 남길 것:
- 커밋 목록 또는 diff 요약
- 줄 수 변화:

```bash
wc -l src/tunapi/discord/loop.py src/tunapi/tunadish/backend.py
```

- 실행한 검증 명령과 결과
- 유지한 wrapper/호환 경로 목록
- 아직 남은 800줄 초과 파일과 다음 후보

## 8. 다음 작업 메모

가장 안전한 다음 PR/커밋 단위:
1. `discord/loop.py`에서 attachment/media group helper 모듈 분리
2. `discord/loop.py`에서 context/trigger/thread resolution helper 모듈 분리
3. `tunadish/backend.py`에서 branch handler 모듈 분리
4. `tunadish/backend.py`에서 message retry/save/delete/adopt handler 모듈 분리

각 단계는 “기존 private method wrapper 유지 → 새 모듈 함수 호출 → 관련 테스트 → 커밋” 순서로 진행하면 안전하다.
