# 작업: 3차 deliberation barrier 타임아웃 시 정직한 부분-보고 경로 복원 + 테스트

## 배경
coordinator(Claude)가 3차 barrier(`_require_deliberation_round(session, 3)`)를 기다리다
타임아웃하는 경우, 현재 `bin/telegram-agent-bot.py`는 예외를 그대로 전파시켜 상위
`except Exception` 블록의 일반 오류 메시지로만 처리된다. 이전 초안(`git stash@{0}`에 보존됨)에는
barrier 타임아웃을 잡아서 어떤 역할이 아직 3차를 완료하지 못했는지 구체적으로 보고하는
`_deliberation_barrier_timeout_message` / `_deliberation_round_progress` 헬퍼와 try/except
경로가 있었는데, 이번 diff에서 빠졌다. 이 정직한 부분-보고 경로가 테스트로 커버되지 않았다.

## 원하는 변경 (최소 범위)
`bin/telegram-agent-bot.py`:
- `_require_deliberation_round` 아래에 스태시본과 동일한 두 헬퍼
  `_deliberation_round_progress(session_id, round_number)` (expected/completed/missing/failed
  역할 튜플 반환) 와 `_deliberation_barrier_timeout_message(session_id, round_number, error)`
  (완료된 것처럼 위장하지 않는 경고 메시지 문자열 반환)를 복원한다.
- `handle_message`의 `ROLE == "claude"` coordinator 분기에서, 3차 barrier
  (`await asyncio.to_thread(_require_deliberation_round, deliberation_session_id, 3)`)
  호출을 try/except RuntimeError로 감싸, 타임아웃 시 `deliberation_incomplete = True`로
  표시하고 `_deliberation_barrier_timeout_message(...)` 결과를 `reply`로 사용한다 (기존
  4-역할 coordinator 종합 프롬프트 텍스트는 barrier 통과시에만 그대로 실행).
- `handle_message` 최상단에 `deliberation_incomplete = False` 플래그를 선언하고,
  기존에 리터럴로 박혀 있던 `status="passed"/"handoff_ready"/"completed"/"succeeded"`
  네 곳을 `deliberation_incomplete` 값에 따라 `"waiting"`/`"running"` 계열로
  분기하는 지역 변수(`execution_status`, `final_state` 등)로 바꾼다 — 완료되지
  않은 barrier를 완료로 보고하지 않기 위함.

`tests/test_edge_agent_deliberation.py`에:
- 3차 barrier가 타임아웃(`round_state` != "ready")하는 상황을 재현해
  `_deliberation_barrier_timeout_message`가 완료되지 않은 역할을 실제로 나열하고
  "완료된 것으로 처리하지 않았다"는 문구를 포함하는지 검증하는 단위 테스트를 추가한다.

## 제약
- 기존 4-역할 coordinator 통합 로직은 건드리지 않는다. barrier 실패 시 대체 경로만 추가한다.
- 관련 없는 리팩터링 금지. 최소 diff.

## 완료 조건
- `python3 -m unittest discover -s tests -p 'test_*.py'` 전체 통과.
- 새 테스트가 barrier 타임아웃 시 정직한 부분-보고를 검증한다.
