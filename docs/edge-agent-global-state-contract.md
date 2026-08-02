# 엣지 에이전트 전역 상태·락 계약

상태: 설계 확정 전의 기준안
작성일: 2026-07-31

이 계약은 현재의 단일 workspace 직렬 실행을 안전하게 유지하면서,
향후 Codex worktree 병렬화를 시작하기 전에 지켜야 할 최소 규칙을 정한다.

## 1. 현재 실행 정책

- 한 저장소의 실제 작업은 한 번에 하나만 실행한다.
- worktree를 사용한 병렬 Codex 실행은 이 문서의 계약을 구현하고 검증하기 전까지 금지한다.
- Team OS 경로는 provider CLI sandbox에서 쓰기를 차단한다.
- watchdog의 `claude-main` 세션은 이 계약의 적용 대상이 아니다. 현재 결정은
  독립 유지이며, 별도 workspace·rollback·smoke test와 사용자 승인 없이는 변경하지 않는다.

## 2. 락 키 규칙

락 키를 계산하는 공통 읽기 전용 helper(`bin/edge_agent_locks.py`)를 추가했다.
일반 checkout과 worktree를
구분해 공통 Git 디렉터리의 canonical root를 사용하고, Git 저장소가 아닌 경로는
기존 호환성을 위해 resolved path로 fallback한다.

```text
lock_key = realpath(repository_common_root)
lock_file = ~/.claude/discord-bot/repo-locks/<sha256(lock_key)>.lock
```

다음 조건을 모두 만족하지 않으면 병렬 실행을 허용하지 않는다.

1. 일반 checkout과 모든 worktree가 동일한 canonical root로 해석된다.
2. lock 획득 실패가 명확한 `busy` 결과를 반환한다.
3. 프로세스 종료·timeout·SIGKILL 뒤에도 OS가 lock을 자동 해제하는지 테스트한다.
4. 원본 브랜치와 대상 파일 집합을 확인한 뒤에만 병합한다.

## 3. 전역 원장 계약

전역 원장은 작업 결과의 권위 있는 기록이다. worktree별 임시 결과와 섞지 않는다.

| 원장 | 기록 단위 | 병렬화 전 필수 조건 |
|---|---|---|
| `nano-gate-events.jsonl` | `taskId + stepId` 멱등 이벤트 | append lock 또는 단일 writer, 중복·충돌 판정 |
| `verify-task-v2-history.jsonl` | 검증 실행 결과 | 원자 append, 손상 줄 감지, 실패 시 성공으로 처리 금지 |
| Retired Discord pending JSON | 퇴역 전 재시도 작업 1건 | 보존만 수행; 새 writer·자동 재시도는 없음 |

원장 기록 실패는 해당 작업을 성공으로 확정하지 않는다. 특히 nano 이벤트
기록이 실패하면 다음 nano step으로 진행하지 않는다.

## 4. 임시 파일과 정리

- JSON·JSONL을 직접 덮어쓰지 않고 같은 디렉터리의 임시 파일에 기록한 뒤 atomic rename한다.
- lock 파일 자체는 삭제하지 않는다. `flock`의 소유권 해제가 생명주기를 결정한다.
- pending 작업과 로그의 자동 정리는 보존기간, 대상 패턴, 실행 주체를 문서화한 뒤에만 한다.
- Team OS의 기존 미커밋 파일·백업 tar·임시 worktree는 이 계약의 정리 대상이 아니다.

## 5. 병렬 worktree 활성화 조건

다음 증거가 모두 있어야 한다.

- canonical-root lock 구현 및 경쟁 테스트
- 전역 원장 동시 append 테스트
- provider 실패·timeout·강제 종료 후 lock 회수 테스트
- worktree 생성·작업·검증·병합·정리의 rollback 시나리오
- Team OS 보호 경로가 모든 worktree provider 프로세스에서도 차단되는지 확인
- 실제 운영을 모사한 저위험 파일럿과 사후 `git status` 검증

그 전까지는 나노 작업을 분할하더라도 실행은 직렬로 유지한다. 작업을 여러 개
계획하는 것과 여러 프로세스를 동시에 쓰게 하는 것은 별개의 기능이다.

## 결정 상태

- 현재: 직렬 실행 유지
- Codex sandbox: 내부 `workspace-write` 사용, legacy shared workspace는 실행 거부
- canonical-root lock: 필요성 확정, 구현 전
- 전역 원장 단일 writer/append lock: 필요성 확정, 구현 전
- worktree 병렬 실행: 비활성
- watchdog Claude sandbox: 별도 정책 결정 전 보류

## worktree 파일럿 결과

2026-07-31 임시 Git 저장소에서 두 worktree가 동시에 실행을 요청하는 파일럿을
수행했다. 첫 worktree가 canonical-root lock을 보유하면 두 번째 worktree는
`busy`로 거부되고, 첫 작업만 파일을 기록했다. 따라서 현재 구현은 “동시 쓰기”가
아니라 “병렬 요청의 안전한 직렬화”를 보장한다.

이 결과에 따라 실제 운영 worktree 병렬 실행은 아직 활성화하지 않는다. 진정한
병렬화를 원하면 작업별 독립 저장소·전역 원장 단일 writer·병합 계약을 별도로
설계해야 한다.
