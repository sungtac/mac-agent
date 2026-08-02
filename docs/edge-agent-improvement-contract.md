# 새 사실·차단 → 개선 작업 계약

하네스가 provider나 환경의 새 사실을 발견하고 작업을 차단할 때, 차단 사유만
반환하는 것은 완료된 handoff가 아니다. 반드시 bounded evidence와 다음 개선
작업을 함께 남겨야 한다.

## 강제 규칙

- `passed=false`인 `verify-task-orchestrator` 결과에는 `improvement_task`가
  자동으로 붙는다.
- provider pilot의 entrypoint·prompt·worktree·capability·usage gate 차단도
  동일한 개선 원장에 queued task를 기록한다.
- 개선 작업 기록 실패는 성공으로 승격되지 않고 `improvement_task_persist_failed`
  로 fail-closed한다.
- provider transcript·credential·원문 출력은 개선 작업에 넣지 않는다. 원인 요약,
  검사명, bounded evidence만 기록한다.
- 동일 사실은 deterministic `task_id`로 멱등 처리한다. 같은 ID의 다른 내용은
  충돌로 거부한다.

## 저장·상태

- 기본 원장: `~/.edge-agent/improvements/tasks.jsonl`
- override: `EDGE_AGENT_IMPROVEMENT_ROOT`
- 디렉터리·파일 권한: private/owner-only
- 신규 작업 상태: `queued`
- 재검증 증거가 저장되면 원자적으로 `completed`로 닫을 수 있다.
- 원래 작업은 개선 작업과 재검증 증거가 생길 때까지 `blocked`로 유지한다.

구현은 `bin/edge_agent_improvement.py`, 하네스 연결은
`bin/verify-task-harness.py`와 `bin/verify-task-orchestrator.py`가 소유한다.
`ImprovementStore.mark_completed()`는 task ID와 bounded revalidation evidence를
검증한 뒤 완료 상태를 멱등적으로 기록한다.
