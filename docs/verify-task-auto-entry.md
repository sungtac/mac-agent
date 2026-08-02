# verify-task-v2 자동 진입 게이트

## 목적

코드 작업에서 `verify-task-v2`가 종료 시점에만 검사되는 문제를 줄인다. 사용자 프롬프트에서 코드 변경 의도가 감지되면 세션 상태를 `gate_required`로 만들고, 메인 세션의 `Edit`·`Write`는 성공한 `verify-task-v2` 결과가 기록될 때까지 차단한다.

## 실행 순서

1. `UserPromptSubmit`의 `verify-task-intent-submit.sh`가 프롬프트를 빠르게 분류한다.
2. 코드 변경 의도이면 `~/.claude/hooks-state/verify-task-v2/<session_id>.json`에 상태를 기록하고 호스트 오케스트레이터 실행 지시를 추가한다.
3. `PreToolUse(Edit|Write)`의 `verify-task-pre-edit-gate.sh`가 메인 세션의 직접 편집을 차단한다.
4. 메인 세션이 Bash로 `python3 /Users/edge_ai/mac-agent/bin/verify-task-orchestrator.py --task-file ... --cwd ... --session-id ...`를 호출한다. 결정론적 하네스와 구독형 provider CLI 호출은 호스트 프로세스가 직접 관리한다.
5. 오케스트레이터가 `final-verdict.json`을 기록하고 `session-id` 상태를 직접 `workflow_completed` 또는 `workflow_failed`로 전환한다. 기존 `PostToolUse(Workflow)` 훅은 구형 어댑터 제거 후 no-op 호환 파일로만 남아 상태를 변경하지 않는다.
6. 기존 `verify-task-stop-check.sh`는 최종 감사 장치로 계속 실행한다.

## 안전 규칙

- Hook에서 별도 `claude -p` 프로세스를 실행하지 않는다. 별도 세션은 현재 Workflow 호출 기록과 분리되고 중복 실행을 만들 수 있다.
- `agent_id`가 있는 내부 서브에이전트의 임시 파일 작성은 허용한다. 이 예외가 없으면 Workflow 내부의 조사·실행 단계가 자기 자신에 의해 차단된다.
- 상태에는 원문 프롬프트를 저장하지 않고 `session_id`, `cwd`, `prompt_hash`, 결과 상태만 저장한다.
- 오케스트레이터의 `--cwd`가 현재 세션의 작업 디렉토리와 다르거나 작업 파일이 비어 있으면 성공 결과를 상태에 반영하지 않는다.
- 오케스트레이터 호출만으로는 통과시키지 않는다. 결과를 해석할 수 없거나 `passed`가 아니면 `workflow_failed`로 닫힌 상태를 유지한다.
- 세션 정보가 없는 메인 `Edit`·`Write` 입력은 실패 폐쇄한다. 상태 기록 잠금은 프로세스가 중단돼도 오래된 잠금을 회수할 수 있다.
- 기존 병렬 Worktree 실행기는 별도 계약을 충족하는 경우에만 선택적으로 사용하며, 이 자동 진입 단계에서 다중 Writer를 켜지 않는다.
