# Operational Resilience & Observability Hardening

**Version:** 1.0
**Status:** Draft
**Created:** 2026-04-05

## Description

통합 분석 보고서에서 지적된 **"죽지 않지만 조용히 상태를 잃는"** 패턴을 6개 독립 서브태스크로 해소한다.
각 서브태스크는 **기존 정상 동작을 변경하지 않으며**, 장애 경로에만 방어 코드를 추가한다 (additive-only).

## Expected Outcome

- 전원 손실·파일 손상 시 데이터 복구 경로 확보
- 락 파일 경합 시 원자적 거부
- 진행 메시지에 컨텍스트 소스 표시 (사용자 신뢰도 향상)
- Doctor 진단 시 수정 가이드 제공
- Roundtable 첫 라운드 병렬 실행 옵션 (opt-in)

## Subtask Summary

| # | Slug | Title | Depends | Parallel Group |
|---|------|-------|---------|----------------|
| 1 | `fsync-atomic-write` | atomic_write_json에 fsync 추가 | — | A |
| 2 | `state-corruption-backup` | 상태/저널 손상 시 격리·백업 | Task 1 | B |
| 3 | `lockfile-atomic` | 락 파일 원자적 획득 | — | B |
| 4 | `context-transparency` | 진행 메시지 컨텍스트 소스 표시 | — | C |
| 5 | `doctor-diagnostics` | Doctor 오류 분류 + 수정 가이드 | — | C |
| 6 | `roundtable-parallel-first` | Roundtable 첫 라운드 병렬 옵션 | — | D |

**실행 순서:** 1 → 2, 3 (병렬 가능) → 4, 5 (병렬 가능) → 6

## Constraints

- 각 서브태스크는 독립 커밋으로 분리
- 기존 테스트가 깨지지 않아야 함 (`uv run pytest --no-cov` 통과)
- 기본값/기본 동작 변경 금지 (additive-only)
- Discord 전용 `_atomic_write_json` 복사본은 이번 범위에서 제외

## Non-goals

- Outbox heapq 최적화 (현재 큐 크기에서 불필요)
- Transport 간 코드 중복 제거 (대규모 리팩토링)
- Runner 믹스인 → 컴포지션 전환
- 프롬프트 인젝션 방어 (엔진 자체 보안과 중복)
- Python 버전/CI 매트릭스 정리 (governance 이슈)

## Task Files

- [Task 01: fsync-atomic-write](operational-resilience-observability-hardening-task-01.md)
- [Task 02: state-corruption-backup](operational-resilience-observability-hardening-task-02.md)
- [Task 03: lockfile-atomic](operational-resilience-observability-hardening-task-03.md)
- [Task 04: context-transparency](operational-resilience-observability-hardening-task-04.md)
- [Task 05: doctor-diagnostics](operational-resilience-observability-hardening-task-05.md)
- [Task 06: roundtable-parallel-first](operational-resilience-observability-hardening-task-06.md)
