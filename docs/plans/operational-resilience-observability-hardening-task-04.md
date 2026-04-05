# Task 04: context-transparency

**Plan:** Operational Resilience & Observability Hardening
**Slug:** `context-transparency`
**Parallel Group:** C
**Depends On:** (none — 독립 UX 개선)

## Changed Files

| File | Action |
|------|--------|
| `src/tunapi/progress.py:22-28` | Modify — `ProgressState`에 `context_source` 필드 추가 |
| `src/tunapi/progress.py:108-127` | Modify — `snapshot()`에 `context_source` 파라미터 추가 |
| `src/tunapi/markdown.py:49-57` | Modify — `format_header()`에 `context` 파라미터 추가 |
| `src/tunapi/markdown.py:201-217` | Modify — `render_progress_parts()`에서 `context_source` 전달 |
| `src/tunapi/runner_bridge.py:214-216` | Modify — `snapshot()` 호출 시 `context_source` 전달 |
| `src/tunapi/runner_bridge.py:272-274` | Modify — `send_initial_progress` 내 `snapshot()` 호출 수정 |

## Change Description

진행 메시지 헤더에 컨텍스트 결정 소스를 표시한다.

**현재 출력:**
```
working · claude/opus4.6 · 3s · step 2
```

**변경 후 출력 (context가 있을 때만):**
```
working · claude/opus4.6 · backend(ambient) · 3s · step 2
```

### 1. `progress.py` — ProgressState 확장

`ProgressState` dataclass에 optional 필드 추가:

```python
@dataclass(frozen=True, slots=True)
class ProgressState:
    engine: str
    action_count: int
    actions: tuple[ActionState, ...]
    resume: ResumeToken | None
    resume_line: str | None
    context_line: str | None
    context_source: str | None = None   # ← 신규
```

`snapshot()` 메서드에 파라미터 추가:

```python
def snapshot(
    self,
    *,
    resume_formatter: Callable[[ResumeToken], str] | None = None,
    context_line: str | None = None,
    context_source: str | None = None,   # ← 신규
) -> ProgressState:
    ...
    return ProgressState(
        ...,
        context_line=context_line,
        context_source=context_source,   # ← 전달
    )
```

### 2. `markdown.py` — format_header 확장

```python
def format_header(
    elapsed_s: float,
    item: int | None,
    *,
    label: str,
    engine: str,
    context: str | None = None,   # ← 신규 (optional, 기본 None)
) -> str:
    elapsed = format_elapsed(elapsed_s)
    parts = [label, engine]
    if context:
        parts.append(context)
    parts.append(elapsed)
    if item is not None:
        parts.append(f"step {item}")
    return HEADER_SEP.join(parts)
```

`render_progress_parts()` 내에서 `context` 인자 전달:

```python
def render_progress_parts(self, state, *, elapsed_s, label="working"):
    step = state.action_count or None
    # context_source + context_line에서 표시 문자열 생성
    ctx_display = _format_context_display(state.context_line, state.context_source)
    header = format_header(
        elapsed_s, step, label=label, engine=state.engine, context=ctx_display
    )
    ...
```

신규 헬퍼:

```python
def _format_context_display(
    context_line: str | None,
    context_source: str | None,
) -> str | None:
    """컨텍스트 라인과 소스를 결합하여 표시 문자열 생성."""
    if not context_line:
        return None
    # context_line은 이미 "`alias`" 형태일 수 있음 — 백틱 제거
    clean = context_line.strip("`").strip()
    if not clean:
        return None
    if context_source and context_source != "none":
        return f"{clean}({context_source})"
    return clean
```

### 3. `runner_bridge.py` — context_source 전달

`ProgressEdits._run_progress_loop()` (line 214):
```python
state = self.tracker.snapshot(
    resume_formatter=self.resume_formatter,
    context_line=self.context_line,
    context_source=self.context_source,   # ← 신규
)
```

`ProgressEdits` dataclass에 `context_source: str | None = None` 필드 추가.

`send_initial_progress()` (line 272):
```python
state = tracker.snapshot(
    resume_formatter=resume_formatter,
    context_line=context_line,
    context_source=context_source,   # ← 신규 파라미터
)
```

`send_initial_progress()` 시그니처에 `context_source: str | None = None` 추가.

### 기존 동작 보존

- `context_source`가 `None`이면 (기본값) 기존 헤더와 동일 출력
- 6개 Presenter 구현체 모두 `MarkdownFormatter`를 사용하므로 자동 적용
- `render_final_parts()`도 동일 패턴 적용 (선택적 — 최종 메시지에서도 표시할지는 구현자 판단)

## Dependencies

- 패키지: 없음
- 다른 subtask: 없음
- 기존 인프라: `ContextSource` 타입이 `transport_runtime.py:31-37`에 이미 정의됨

## Verification

```bash
# 1. 타입 체크
uv run ty check src/tunapi/progress.py src/tunapi/markdown.py src/tunapi/runner_bridge.py

# 2. progress/markdown 관련 테스트
uv run pytest tests/ -k "progress or markdown or presenter or render" --no-cov -x -q

# 3. 전체 테스트 (additive 변경이므로 기존 동작 불변 확인)
uv run pytest tests/ --no-cov -x -q

# 4. 신규 필드/함수 존재 확인
grep -n "context_source" src/tunapi/progress.py
grep -n "context.*display" src/tunapi/markdown.py
```

## Risks

- **헤더 길이 증가:** `context(source)` 추가로 모바일 환경에서 줄바꿈 가능성. 단, `shorten()` 함수로 향후 제어 가능하며, 이번 변경에서는 짧은 문자열만 추가됨 (예: `backend(ambient)` = 16자).
- **테스트 정확한 문자열 매칭:** 기존 테스트가 헤더 문자열을 exact match하면 실패 가능. `context_source=None`이 기본이므로 기존 테스트에 영향 없음.

## Scope Boundary (수정 금지)

- `src/tunapi/transport_runtime.py` — `ContextSource` 타입 정의 불변
- `src/tunapi/telegram/loop_dispatch.py` — Telegram의 기존 `context_source` 활용 로직 불변
- 6개 Presenter 구현체 (`telegram/bridge.py`, `discord/bridge.py`, `tunadish/presenter.py` 등) — `MarkdownFormatter` 사용하므로 개별 수정 불필요
