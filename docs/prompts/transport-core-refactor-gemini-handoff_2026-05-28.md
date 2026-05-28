---
title: Transport Core 통합 및 비대 파일 분해 리팩토링 — Gemini/Codex Verification
type: prompt
status: completed
priority: P1
updated_at: 2026-05-29
owner: codex
paired_plan: docs/plans/transport-core-consolidation-and-fatfile-decomposition.md
expected_output: 비대 파일 분해, test_coverage_push.py 이주/삭제, tests/fakes/ 추출, pytest coverage 83% 기준 통과
---

# Transport Core Refactor — Verification Handoff

## 1. Context & Status (현재 상태 요약)

Codex에 이어 Gemini가 세션을 이어받아 Discord/Tunadish 비대 파일 분해 작업에 더해, **Presenter 구조 통합 및 문서화(Task 08)**, **Test Fake 모듈 추출 및 이주(Task 09)**, **test_coverage_push.py 분산 이주/삭제 및 커버리지 상향(Task 10)**까지 진행했습니다. 2026-05-29 Codex 재검증 결과, 전체 pytest는 현재 `pyproject.toml` 기준인 `--cov-fail-under=83`을 통과합니다.

- **브랜치 상태**: `main...origin/main [ahead 33]`, 작업 트리 dirty. `debug.log`는 변경되어 있으나 커밋 대상에서 제외해야 합니다.
- **최종 pytest 검증**: `uv run pytest tests/ -q` -> `3641 passed, 3 warnings`, coverage `83.10%`, required `83%` reached.
- **전체 Ruff 검증**: `uv run ruff format --check src tests` 및 `uv run ruff check src tests` 통과.
- **주의**: `just check`는 설치 후 실행 확인했으며 Ruff 단계는 통과하지만, `uv run ty check src tests`에서 전체 레포 기준 기존 타입 진단이 다수 남아 실패합니다. 타입 정리는 별도 범위로 분리하는 편이 안전합니다.

---

## 2. 주요 리팩토링 및 이주 결과

### 1) Presenter 구조 통합 및 문서화 (Task 08)
- Discord `bridge.py` 내부 `DiscordPresenter`를 `ChatPresenter` 상속 구조로 이식하여 인터페이스 통일을 이룸.
- Telegram 및 Tunadish에는 아키텍처 상의 예외 주석을 상세 기술함.

### 2) Test Fake 모듈 추출 및 이주 (Task 09)
각 채널별 단위 테스트에 중복 정의되어 있던 가짜 객체 및 헬퍼 빌더들을 [tests/fakes/](file:///Users/d9ng/privateProject/tunapi/tests/fakes) 아래로 통합하였습니다:
- **Slack Fake ([slack.py](file:///Users/d9ng/privateProject/tunapi/tests/fakes/slack.py))**: `_make_msg`, `_make_cfg`, `_make_resolved_message`, `_make_resolved_runner`
- **Mattermost Fake ([mattermost.py](file:///Users/d9ng/privateProject/tunapi/tests/fakes/mattermost.py))**: `_make_msg`, `_make_cfg`, `_make_resolved_message`, `_make_resolved_runner`
- **Tunadish Fake ([tunadish.py](file:///Users/d9ng/privateProject/tunapi/tests/fakes/tunadish.py))**: `FakeWs`, `FakeRuntime`
- **Discord Fake ([discord.py](file:///Users/d9ng/privateProject/tunapi/tests/fakes/discord.py))**: `_make_cfg`, `_make_running_task`, `FakeBot`, `FakeBotClient`, `FakeClock`, `FakeSleep`, `FakeAttachment`, `FakeAttachmentOSError`

이에 따라 다음 단위 테스트들이 신규 fakes 패키지를 임포트하여 사용하도록 수정되어 중복 제거 및 리팩토링을 완료했습니다:
- [test_slack_loop_dispatch.py](file:///Users/d9ng/privateProject/tunapi/tests/test_slack_loop_dispatch.py)
- [test_mm_loop_dispatch.py](file:///Users/d9ng/privateProject/tunapi/tests/test_mm_loop_dispatch.py)
- [test_tunadish_backend_extra.py](file:///Users/d9ng/privateProject/tunapi/tests/test_tunadish_backend_extra.py)
- [test_discord_loop_helpers.py](file:///Users/d9ng/privateProject/tunapi/tests/test_discord_loop_helpers.py)
- [test_discord_backend.py](file:///Users/d9ng/privateProject/tunapi/tests/test_discord_backend.py)
- [test_discord_handlers_commands.py](file:///Users/d9ng/privateProject/tunapi/tests/test_discord_handlers_commands.py)
- [test_discord_client.py](file:///Users/d9ng/privateProject/tunapi/tests/test_discord_client.py)
- [test_discord_outbox.py](file:///Users/d9ng/privateProject/tunapi/tests/test_discord_outbox.py)
- [test_discord_file_put.py](file:///Users/d9ng/privateProject/tunapi/tests/test_discord_file_put.py)
- [test_discord_file_transfer_extra.py](file:///Users/d9ng/privateProject/tunapi/tests/test_discord_file_transfer_extra.py)

---

## 3. Verification & Lint

- **Lint 통과 검증**:
  수정 및 생성된 Python 파일 기준 Ruff 포맷/검사를 완료했습니다.
- **테스트 통과 검증**:
  ```bash
  uv run pytest tests/ -q
  ```
  결과: `3641 passed, 3 warnings`, coverage `83.10%` (`--cov-fail-under=83` 통과)

- **전체 check 주의사항**:
  ```bash
  just check
  ```
  결과: Ruff format/check 단계 통과 후 `uv run ty check src tests`에서 기존 타입 진단으로 중단. `just`는 `/opt/homebrew/bin/just`로 설치 완료.
