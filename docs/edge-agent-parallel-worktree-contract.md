# 엣지 에이전트 병렬 Worktree 계약

작성일: 2026-07-31
상태: 설계만 확정·운영 미활성화

## 1. 목적과 범위

이 문서는 서로 충돌하지 않는 나노 작업을 별도 Git worktree에서 병렬 실행하기
위한 최소 계약을 정의한다. 현재 운영은 안전한 직렬 실행이며, 이 문서만으로
병렬 실행을 활성화하지 않는다.

Team OS와 OpenClaw의 라우팅·승인권한을 가져오지 않는다. 엣지 에이전트 내부의
작업 격리, 전역 기록, 통합, 실패 복구만 다룬다.

## 2. 현재 구조와 전제

- 저장소 락은 worktree의 실제 경로가 아니라 `canonical_repository_root`를
  기준으로 잡아 같은 원본 저장소의 경쟁을 감지한다.
- nano 이벤트 원장은 별도 JSONL과 sidecar lock으로 멱등 append를 보장한다.
- Telegram Codex에는 작업별 detached worktree 생성 경로가 있으나, 모든 채널과
  전체 병렬 작업을 위한 lifecycle·merge·rollback 계약은 아직 없다.
- provider가 Team OS 공유 workspace를 직접 쓰는 방식은 병렬 실행의 대상이 아니다.

## 3. Worktree lifecycle 계약

1. 기준 저장소는 `canonical_repository_root`로 먼저 확정한다.
2. 작업 ID는 재사용하지 않는 안정적인 식별자여야 한다.
3. worktree 경로는 다음 형식으로 고정한다.

   `~/.edge-agent-worktrees/<repo-hash>/<task-id>/`

4. 생성은 원본 저장소 lifecycle lock을 획득한 뒤 `git worktree add --detach
   <path> <base-commit>`으로 수행한다.
5. 작업 manifest를 atomic rename으로 기록한다. 필수 필드는 `taskId`,
   `repoRoot`, `baseCommit`, `worktreePath`, `owner`, `declaredFiles`,
   `state`, `createdAt`이다.
6. provider는 worktree만 쓰며 원본 checkout과 Team OS 보호 경로에는 쓰지 않는다.
7. 성공·실패·취소·고아 복구가 끝난 뒤에만 worktree를 제거한다. manifest와
   이벤트 원장은 제거하지 않는다.

## 4. 락 계층

| 계층 | 보호 대상 | 정책 |
|---|---|---|
| 저장소 lifecycle lock | `git worktree add/remove`, ref 조작 | 짧게 획득하고 즉시 실패. 대기·자동 queue 금지 |
| task worktree lock | 한 worktree의 provider 실행 | 한 작업당 단일 writer |
| conflict reservation | 선언 파일·의존성 경계 | 겹치면 병렬 실행 거부 |
| global writer lock | nano 원장·usage·pending JSONL | append/atomic rename만 직렬화 |
| integration lock | 기준 checkout 반영 | 항상 한 번에 하나만 merge |

worktree마다 다른 경로의 락을 만들더라도 conflict reservation과 integration
lock은 canonical repository 기준이어야 한다. 경로 해시만으로 병렬 안전성을
판정하지 않는다.

## 5. 병렬 실행 허용 조건

다음 조건을 모두 만족할 때만 병렬 실행을 허용한다.

- 모든 작업이 같은 base commit에서 생성됐다.
- 각 작업의 `declaredFiles`가 서로 겹치지 않는다.
- 의존성 edge가 같은 경계를 가로지르지 않는다.
- 민감 경로·Team OS 보호 경로·전역 설정 파일을 변경하지 않는다.
- 각 작업이 독립적인 event idempotency key를 가진다.
- provider headroom 게이트가 각 작업 시작 시 통과한다.
- 하나의 작업 실패가 다른 worktree의 성공 판정을 바꾸지 않는다.

파일 목록을 알 수 없거나 도구가 실제 변경 목록을 반환하지 않으면 병렬이
아니라 직렬 경로로 강등한다. “서로 다른 worktree”만으로 충돌 없음으로
판정하지 않는다.

## 6. 전역 원장 계약

- 모든 이벤트에는 `taskId`, `stepId`, `worktreePath`, `baseCommit`, `status`,
  `changedFiles`를 기록한다.
- 같은 `taskId::stepId`의 동일 payload는 duplicate로 처리한다.
- 같은 키의 다른 payload는 충돌로 처리하고 다음 단계 진행을 막는다.
- append 실패·손상 줄·lock timeout은 성공으로 바꾸지 않는다.
- provider 프로세스의 `exitCode=0`은 작업 성공이 아니다. 실제 diff와 검증
  결과가 있어야 `passed`를 기록한다.
- 원장 writer는 하나의 API로 제한하며 provider가 직접 JSONL을 수정하지 못하게
  한다.

## 7. Merge 계약

각 worktree는 다음을 모두 통과해야 integration 단계로 이동한다.

1. provider 실행 완료 및 실제 변경 목록 확보
2. declared files와 actual diff의 범위 일치
3. nano light/mid/full 검증 통과
4. 테스트·문법·`git diff --check` 통과
5. 보호 경로 및 예상 밖 파일 변경 없음
6. base commit과 기준 checkout의 관계 재확인
7. integration lock 획득

통합은 기준 checkout에서 한 번에 하나만 수행한다. 기준 checkout이 사용자
미커밋 변경으로 dirty하면 자동 merge·stash·reset을 하지 않고 `integration_blocked`
상태로 멈춘다. 충돌을 자동 해결하거나 사용자 변경을 덮어쓰지 않는다.

## 8. Rollback과 복구 계약

- 아직 merge하지 않은 작업은 worktree를 보존한 채 `failed` 또는 `cancelled`로
  기록하고, 사용자 승인 없이 삭제하지 않는다.
- merge 중 실패하면 기준 checkout을 자동 reset하지 않는다. merge 중단 상태와
  원장·manifest를 남기고 수동 복구 대상으로 전환한다.
- 이미 반영된 변경의 되돌림은 별도 revert commit만 허용하며 자동 실행하지 않는다.
- 프로세스가 죽어도 manifest를 기준으로 orphan worktree를 탐지한다.
- 고아 정리는 `state=finished|failed|cancelled`이고 provider PID가 없으며,
  보존 기간이 지난 경우에만 별도 cleanup 명령으로 수행한다.

## 9. 구현 전 수용 테스트

1. 서로 다른 두 파일의 두 worktree가 동시에 실행되고 각각 독립 event를 남긴다.
2. 같은 파일을 선언한 두 작업 중 하나가 `conflict_busy`로 즉시 거부된다.
3. worktree 경로가 달라도 canonical repository lifecycle lock이 충돌을 감지한다.
4. 한 provider가 실패해도 다른 worktree의 결과와 원장은 손상되지 않는다.
5. 동일 이벤트 재시도는 한 줄만 남기고, 다른 payload는 충돌한다.
6. 중단된 작업이 merge되지 않고 manifest와 로그를 보존한다.
7. dirty 기준 checkout, Team OS 보호 경로, 민감 파일은 integration을 거부한다.
8. 실제 병렬 provider 실행 전에 모든 테스트가 token-free fake provider로 통과한다.

## 10. 단계적 도입 순서

1. manifest·conflict reservation·global writer 계약과 token-free 테스트 구현
2. fake provider 세 개로 병렬 worktree contract pilot 수행
3. merge·rollback·orphan recovery를 별도 테스트
4. 실제 provider 한 개의 worktree 실행을 직렬 mode로 검증
5. 성공 표본과 운영 승인 후에만 제한된 병렬 pilot 검토

token-free fake provider 기준의 실행·자동 merge 계약은 구현되어 있다. 운영에서
자동 merge를 사용하려면 `ParallelPipeline(..., parallel_enabled=True,
automatic_merge=True, approval_ref=..., approval_checker=...)`를 명시적으로
선택해야 하며, 승인 reference 또는 검증기가 없거나 검증에 실패하면 provider
시작 전에 `ParallelApprovalRequired`로 fail-closed 한다. 이 모드에서도 대상
checkout이 dirty하거나 provider 결과가 실패·범위 초과·diff 오류·충돌이면
병합하지 않고 worktree와 상태를 보존한다. 기존 Telegram·Discord 라우팅과
launchd에는 아직 연결하지 않는다.
