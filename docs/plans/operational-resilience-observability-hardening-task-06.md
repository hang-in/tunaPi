# Task 06: roundtable-parallel-first

**Plan:** Operational Resilience & Observability Hardening
**Slug:** `roundtable-parallel-first`
**Parallel Group:** D
**Depends On:** (none — 독립 기능, 마지막 실행 권장)

## Changed Files

| File | Action |
|------|--------|
| `src/tunapi/settings.py:144-150` | Modify — `RoundtableSettings`에 `parallel_first_round` 추가 |
| `src/tunapi/transport_runtime.py:24-29` | Modify — `RoundtableConfig`에 `parallel_first_round` 추가 |
| `src/tunapi/core/roundtable.py:303-400` | Modify — `_run_single_round()`에 병렬 분기 추가 |
| `src/tunapi/core/roundtable.py:403-455` | Modify — `run_roundtable()`에서 병렬 옵션 전달 |

## Change Description

Roundtable 첫 라운드에서 이전 컨텍스트가 없으므로 엔진들을 병렬 실행할 수 있다.
**기본값 `False`**이므로 기존 동작은 완전히 보존된다.

### 1. `settings.py` — RoundtableSettings 확장

```python
class RoundtableSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    engines: list[NonEmptyStr] = Field(default_factory=list)
    rounds: int = Field(default=1, ge=1)
    max_rounds: int = Field(default=3, ge=1)
    parallel_first_round: bool = False   # ← 신규
```

### 2. `transport_runtime.py` — RoundtableConfig 확장

```python
@dataclass(frozen=True, slots=True)
class RoundtableConfig:
    engines: tuple[str, ...]
    rounds: int = 1
    max_rounds: int = 3
    parallel_first_round: bool = False   # ← 신규
```

### 3. `core/roundtable.py` — 병렬 실행 분기

`_run_single_round()` 시그니처에 `parallel: bool = False` 추가:

```python
async def _run_single_round(
    session: RoundtableSession,
    topic: str,
    engines: list[str],
    *,
    cfg: RoundtableBridgeCfg,
    running_tasks: RunningTasks,
    ambient_context: RunContext | None,
    parallel: bool = False,   # ← 신규
) -> list[tuple[str, str]]:
```

**parallel=True 분기 (첫 라운드 전용):**

```python
if parallel and len(engines) > 1:
    return await _run_round_parallel(
        session, topic, engines,
        cfg=cfg, running_tasks=running_tasks,
        ambient_context=ambient_context,
    )
# 기존 순차 로직 유지
for engine_id in engines:
    ...
```

**신규 함수:**

```python
async def _run_round_parallel(
    session: RoundtableSession,
    topic: str,
    engines: list[str],
    *,
    cfg: RoundtableBridgeCfg,
    running_tasks: RunningTasks,
    ambient_context: RunContext | None,
) -> list[tuple[str, str]]:
    """첫 라운드 엔진들을 병렬 실행하고 결과를 수집."""
    results: dict[str, str] = {}

    async def _run_one(engine_id: str) -> None:
        if session.cancel_event.is_set():
            return
        # 기존 _run_single_round의 for 루프 본문과 동일한 로직
        runtime = cfg.runtime
        transport = cfg.exec_cfg.transport
        send_opts = SendOptions(thread_id=session.thread_id)

        prompt = _build_round_prompt(
            topic, session.transcript, session.current_round,
            current_round_responses=[],  # 첫 라운드이므로 빈 컨텍스트
        )

        resolved = runtime.resolve_runner(
            resume_token=None, engine_override=engine_id,
        )
        if resolved.issue:
            await transport.send(
                channel_id=session.channel_id,
                message=RenderedMessage(text=f"**[{engine_id}]**: {resolved.issue}"),
                options=send_opts,
            )
            return

        context = ambient_context
        context_line = runtime.format_context_line(context)
        try:
            cwd = runtime.resolve_run_cwd(context)
        except Exception as exc:
            logger.error("roundtable.resolve_cwd_error", error=str(exc))
            await transport.send(
                channel_id=session.channel_id,
                message=RenderedMessage(text=f"{exc}"),
                options=send_opts,
            )
            return

        if cwd:
            bind_run_context(project=context.project if context else None)

        engine_label = f"`{engine_id}`"
        full_context = (
            f"{context_line} | {engine_label}" if context_line else engine_label
        )

        incoming = IncomingMessage(
            channel_id=session.channel_id,
            message_id=session.thread_id,
            text=prompt,
            thread_id=session.thread_id,
        )

        try:
            answer = await handle_message(
                cfg.exec_cfg,
                runner=resolved.runner,
                incoming=incoming,
                resume_token=None,
                context=context,
                context_line=full_context,
                running_tasks=running_tasks,
            )
            if answer:
                results[engine_id] = answer
        except Exception as exc:
            logger.error(
                "roundtable.agent_error",
                engine=engine_id, error=str(exc),
            )
            await transport.send(
                channel_id=session.channel_id,
                message=RenderedMessage(text=f"**[{engine_id}]** error: {exc}"),
                options=send_opts,
            )

    async with anyio.create_task_group() as tg:
        for engine_id in engines:
            tg.start_soon(_run_one, engine_id)

    # 원래 engines 순서 유지
    return [(eid, results[eid]) for eid in engines if eid in results]
```

### 4. `run_roundtable()` 에서 병렬 옵션 전달

```python
# line 435 근처
parallel = (
    round_num == 1
    and getattr(cfg, '_parallel_first_round', False)
)
round_transcript = await _run_single_round(
    session, session.topic, session.engines,
    cfg=cfg, running_tasks=running_tasks,
    ambient_context=ambient_context,
    parallel=parallel,
)
```

실제로는 `run_roundtable()`에 `parallel_first_round: bool = False` 파라미터를 추가하고, 호출부에서 `RoundtableConfig.parallel_first_round` 값을 전달.

### Config 연결

`tunapi.toml` 예시:
```toml
[roundtable]
engines = ["claude", "gemini", "codex"]
rounds = 2
parallel_first_round = true
```

## Dependencies

- 패키지: 없음 (`anyio`는 이미 사용 중)
- 다른 subtask: 없음 (독립 실행 가능)

## Verification

```bash
# 1. 타입 체크
uv run ty check src/tunapi/settings.py src/tunapi/core/roundtable.py src/tunapi/transport_runtime.py

# 2. roundtable 관련 테스트
uv run pytest tests/ -k "roundtable" --no-cov -x -q

# 3. settings 관련 테스트
uv run pytest tests/ -k "settings or config" --no-cov -x -q

# 4. parallel_first_round 필드 존재 확인
grep -n "parallel_first_round" src/tunapi/settings.py src/tunapi/core/roundtable.py src/tunapi/transport_runtime.py

# 5. 전체 테스트
uv run pytest tests/ --no-cov -x -q
```

## Risks

- **Transport 동시 전송:** `handle_message()`가 내부에서 `transport.send()`/`transport.edit()`를 호출. 여러 엔진이 동시에 progress 메시지를 전송하면 메시지 순서가 뒤섞일 수 있음.
  - **완화:** Transport의 outbox queue가 이미 rate limiting을 하고 있으므로 충돌은 방지됨. 메시지 순서만 비결정적이 되나, 각 메시지에 엔진 라벨이 포함되어 구분 가능.
- **cancel_event 경합:** 병렬 실행 중 cancel이 발생하면 일부 엔진만 중단될 수 있음. `anyio.create_task_group`의 특성상 하나가 실패하면 나머지도 취소되므로 문제 없음. `cancel_event` 체크는 각 `_run_one` 시작부에서 수행.
- **메모리:** 엔진 수가 적으므로 (보통 2-4개) 동시 서브프로세스 메모리 부담은 무시 가능.
- **기본값 False:** 설정하지 않으면 기존 순차 실행과 완전히 동일. opt-in이므로 안전.

## Scope Boundary (수정 금지)

- `_build_round_prompt()` — 프롬프트 생성 로직 불변
- `run_followup_round()` — follow-up은 항상 순차 (이전 라운드 컨텍스트 필요)
- `RoundtableSession` — 세션 모델 불변
- `RoundtableStore` — 영속화 로직 불변
- Transport outbox/rate limiting — 기존 동시성 보호 메커니즘에 의존
