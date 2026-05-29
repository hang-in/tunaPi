---
title: tunaDish !rt 구현 — Developer Handoff
paired_plan: ../plans/roundtable-production-enhancement.md
target_agent: claude-main
priority: P1
status: done
created: 2026-05-29
completed: 2026-05-29
expected_output: tunaDish에서 !rt가 동작 (start/follow/close) + 공유 코어(P1~P3 role/synthesis/consensus) 적용 + just check 통과 + 커버리지 ≥83
---

> **완료 (2026-05-29)**: `tunadish/roundtable.py`(배선) + `commands.py:handle_rt`/`dispatch_command` + `backend.py`(RoundtableStore 생성, dispatch 인자 전달, `roundtable.start` RPC 헤더-가로채기 버그 수정). thread_id=conv_id 매핑으로 follow/close 라우팅. 긴 라운드는 `backend._task_group`에 spawn. 신규 테스트 16개(`test_tunadish_roundtable.py` 12 + `test_tunadish_backend_extra.py::TestRoundtableIntegration` 2 + 기존 갱신). 전체 3760 passed, 커버리지 83.24%. **잔여**: `just check`의 `ruff format`/`ty` 실패 8+102건은 이번 작업과 무관한 main 기존 드리프트(내 파일은 모두 통과).

# tunaDish `!rt` 구현 — Developer Handoff

## 1. 목표
`tunadish/commands.py:handle_rt`의 stub("구현 예정")을 실제 동작으로 채운다. tunaDish 채널에서 `!rt "주제"` / `!rt follow [engines] "q"` / `!rt close`가 다른 transport와 동일하게 작동하고, 이번 세션에 추가한 **role 기반 토론 + 구조화 synthesis + cross-round consensus(P1~P3)**를 그대로 누린다.

## 2. 현재 상태 (이미 된 것)
- **엔진은 공유 완성**: `core/roundtable/`(P0 패키지 분해) — `handle_rt`, `run_roundtable`, `run_followup_round`, `RoundtableBridgeCfg`, `RoundtableSession`, `RoundtableStore`. P1 role 주입 / P2 구조화 synthesis(`consensus.py` → SynthesisArtifact 실채움) / P3 cross-round consensus 주입 / P4 save 실패 로깅까지 main에 있음. **transport-agnostic이라 tunaDish가 붙기만 하면 전부 적용됨.**
- **tunaDish 백엔드는 tunapi 안에 있고 필요한 조각 다 보유**:
  - `tunadish/backend.py`: `self._runtime`(TransportRuntime — `.roundtable`/`.resolve_runner`/`available_engine_ids`), `self.running_tasks: dict[MessageRef, RunningTask]`, `self._facade = ProjectMemoryFacade()`.
  - `tunadish/run_handlers.py:164`: 단일 run 경로가 이미 `ExecBridgeConfig`를 조립함 (→ `RoundtableBridgeCfg(runtime, exec_cfg)` 조립 가능).
  - `tunadish/transport.py`: `send(channel_id, message, options=SendOptions(thread_id=...))` — 스레드 지원.
- 다른 transport rt는 mattermost/slack가 레퍼런스: `core/chat_loop_helpers.py:235 dispatch_roundtable_command_flow` + `:672 start_roundtable_thread` + `:114 archive_roundtable_thread`(journal + `facade.save_roundtable(auto_synthesis=True, auto_structured=True)`).

## 3. 작업 (배선)
`tunadish/commands.py:handle_rt`를 공유 `core.roundtable.handle_rt`에 연결:
- `RoundtableBridgeCfg`를 tunaDish의 `runtime` + `ExecBridgeConfig`(run_handlers 패턴 재사용)로 조립.
- start/follow/close 콜백을 tunaDish의 conversation/RPC 모델에 맞게 구현 — `start_roundtable_thread`(세션 생성 + `run_roundtable` + finally `roundtables.complete`) 패턴을 따르되, slack의 thread 대신 tunaDish의 conversation/WS 메시지로.
- close 시 `archive_roundtable_thread`처럼 `self._facade.save_roundtable(...)` 호출 → 영속화(현재 RPC `discussion.save_roundtable`는 client-side transcript용; 서버 run 결과도 저장).
- `RoundtableStore`를 tunaDish용 persist 경로(`~/.tunapi/tunadish_roundtables.json` 등)로 생성.

## 4. 핵심 파일
- 채울 곳: `src/tunapi/tunadish/commands.py` (`handle_rt` stub ~:322).
- 레퍼런스: `src/tunapi/slack/loop.py`(rt 시작/archive, facade 생성 :696), `core/chat_loop_helpers.py`(dispatch/start/archive), `tunadish/run_handlers.py`(`_execute_run`/ExecBridgeConfig 조립).
- 엔진(수정 불필요): `core/roundtable/{commands,orchestrator,session,prompt,roles,consensus}.py`.

## 5. 제약 (Invariants)
- 새 영속화 추가 금지 — 기존 `RoundtableStore` + `facade`(discussion/synthesis/structured) 재사용.
- `core/` 수정 금지(엔진은 완성). transport-specific 배선만 tunadish/에.
- role은 config opt-in(`[roundtable].roles`) — 미설정 시 기존 동작.
- `just check` 통과, `--cov-fail-under=83` 유지, `test_layering`(core→transport 역import 0) 유지.

## 6. Verification
- 단위: tunaDish fake transport로 start/follow/close 흐름 + RoundtableBridgeCfg 조립 테스트(slack/mm rt 테스트 패턴 참고: `tests/test_mattermost_roundtable.py`).
- 통합/smoke: tunaDish 채널에서 `!rt "주제"` 2~3 engine → 응답 스트리밍 확인. `[roundtable].roles=["proposer","reviewer","synthesizer"]`면 synthesizer 출력으로 SynthesisArtifact 채워지는지 확인.
- 회귀: 다른 transport rt 테스트 그대로 통과.

## 7. Open questions / risks
- **conversation↔thread 매핑**: tunaDish는 slack thread가 아니라 conversation ID 기반(자체 `session_store`/`context_store`). rt 세션의 `thread_id`를 tunaDish conversation에 어떻게 매핑할지 결정 필요.
- **WS 클라이언트**: roundtable은 서버에서 돌고 `message.new`/`message.update`로 흐름 → 클라이언트는 그대로 렌더될 가능성 높음. 단 thread/progress 표시가 적절한지 실제 확인(클라이언트 변경 최소화 목표).
- **follow/close over RPC**: tunaDish는 `!rt close`/`follow`가 conversation 내에서 어떻게 트리거되는지(reserved command 라우팅) 확인.
- **worktree 주의**: linked worktree에선 `tests/test_exec_render.py::test_file_change_renders_relative_paths_inside_cwd`가 cwd 경로 상대화로 실패함(아티팩트, main에선 통과). worktree로 작업 시 무시.

## 참고
- 더 깊은 맥락(이번 세션 결정·tunaFlow rt 패턴 조사)은 claude-mem 검색: "roundtable", "tunadish rt", "rt P1/P2/P3".
- tunaFlow 레퍼런스 구현(role/consensus 패턴 원본): `/Users/d9ng/privateProject/tunaFlow/src-tauri/src/commands/roundtable_helpers/`.
