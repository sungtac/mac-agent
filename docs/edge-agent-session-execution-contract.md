# 논리 세션 ↔ Provider 실행 연결 계약

상태: v1 opt-in harness 구현, 운영 런타임 연결 전

## 실행 조건

`SessionParallelRunner`는 다음 조건을 모두 요구한다.

1. 논리 세션 snapshot이 먼저 존재한다.
2. 세션의 `task_id`와 `ParallelTaskSpec.task_id`가 같다.
3. 세션과 작업의 기준 commit이 일치한다.
4. 세션 lease를 획득한다.
5. 명시적으로 `parallel_enabled=True`를 전달한다.
6. provider는 생성된 detached worktree만 사용한다.

실행 전에는 `execution_started`, `worktree_created` 이벤트를 기록하고,
실행 후에는 실제 변경 파일·검증 결과·멱등 이벤트 키를 기록한다.
또한 `edge_agent.team_os_result.v1` 결과로 실행 상태와 병합 상태를 분리해
반환한다. 실행이 성공했지만 아직 병합되지 않은 경우 결과는 `blocked`이며,
논리 세션은 `handoff_ready` 상태로 남는다.

## 실패 정책

- lease 획득 실패 시 provider를 실행하지 않는다.
- task 또는 base commit이 다르면 worktree를 만들지 않는다.
- provider 실패는 세션을 성공으로 바꾸지 않는다.
- 자동 병합은 기본값이 아니며, 사용 시 별도 opt-in과 승인 reference·검증기가
  모두 필요하다. 승인 검증 실패는 provider 시작 전 fail-closed 한다.
- 운영 Telegram·Discord·launchd에는 아직 연결하지 않는다.
