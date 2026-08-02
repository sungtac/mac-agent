# 엣지 에이전트 NanoFlow 프로젝트 결과보고서

작성일: 2026-07-31
상태: 1차 안정화·경계 확정 완료

## 1. 프로젝트 목표

엣지 에이전트(Claude·Codex·Antigravity·Roda)의 독립 운영을 유지하면서,
Team OS와의 충돌을 막고, 나노 작업·락·전역 상태·provider 실행 경계를
안전하게 검증하는 것이 목표였다.

## 2. 완료한 작업

| 영역 | 결과 |
|---|---|
| workspace 경계 | Team OS의 `team_os/`, `state/`, `sukja_telegram/` 보호 경로 확정 |
| provider 보호 | Claude·Codex·Antigravity와 하위 도구의 보호 경로 쓰기 차단 |
| Roda | Ollama 대화 전용, 도구·파일 권한 없음 유지 |
| 전역 상태 감사 | 상태·락·pending·원장 경로를 읽기 전용으로 목록화 |
| nano 원장 | lock·멱등성·fsync·손상 fail-closed 동시성 검증 |
| pending 상태 | Discord 재시도 JSON을 atomic rename 방식으로 기록 |
| 저장소 락 | checkout·worktree 공통 canonical root 기준 락 적용 |
| worktree 보호 | 임시 worktree에서도 Team OS 쓰기 차단 확인 |
| nano 파일럿 | 격리 임시 저장소에서 `passed/light` 실 provider 이벤트 1건 확인 |
| watchdog | `claude-main`은 provider sandbox 밖 독립 세션으로 유지 결정 |
| Team OS 경계 | 어댑터·공유 라우터·공유 승인권한 없이 독립 운영 확정 |

## 3. 검증 결과

- Python 관련 안정화·경계 테스트: 최종 실행 기준 39개 통과
- nano 원장 Node 테스트: 9개 통과
- provider sandbox canary: 보호 경로 차단 및 허용 경로 기록 통과
- worktree lock pilot: 한 작업 획득, 경쟁 작업 `busy` 거부 확인
- Claude·Codex·Anti·Discord·Roda 서비스: 정상 실행 상태 확인
- Team OS 파일: 이번 프로젝트에서 수정·삭제하지 않음

## 4. 핵심 설계 결론

현재 시스템은 “진정한 병렬 실행 플랫폼”이 아니라, 안전한 직렬 실행과
병렬 요청 거부를 보장하는 안정화 단계다. 나노 작업을 여러 개 계획하는 것과
여러 프로세스가 같은 저장소를 동시에 수정하는 것은 분리한다.

실제 병렬 worktree 실행을 활성화하려면 전역 원장 단일 writer, 병합 계약,
작업별 독립 상태, 실패·rollback 절차가 추가로 필요하다.

## 5. 남은 제한사항

1. nano 이벤트는 실 provider 성공 표본이 1건뿐이다. 임계값을 통계적으로 확정할 수 없다.
2. watchdog Claude 세션은 provider sandbox 범위 밖이다.
3. Team OS와 엣지 에이전트 사이의 자동 어댑터는 아직 없다.
4. 현재 worktree 파일럿은 병렬 실행이 아니라 안전한 직렬화 검증이다.
5. 전역 원장과 pending 상태는 안정화됐지만, 대규모 다중 머신 분산 실행을 지원하지 않는다.

## 6. 최종 운영 원칙

- Team OS와 엣지 에이전트는 별개 시스템으로 운영한다.
- Roda는 대화 전용 로컬 모델로 유지한다.
- provider CLI는 보호 경로 sandbox를 통과해야 한다.
- 같은 원본 저장소의 쓰기 작업은 canonical lock으로 직렬화한다.
- 기록 실패·검증 실패·락 충돌은 성공으로 처리하지 않는다.
- 실제 병렬화와 Team OS 연결은 별도 승인과 추가 설계 후에만 진행한다.

## 7. 최신 후속 기록 (2026-07-31)

- 사용량 게이트는 정상이다. Claude 3613 계정의 5시간 잔량이 현재 0%이므로 호스트 브리지 실측은 계정 창 초기화 후로 보류한다.
- Codex 파일럿 실패 원인은 Claude Workflow 샌드박스 안에서 Codex의 `workspace-write` 샌드박스를 중첩 실행한 `sandbox_apply=71`로 확인했다.
- 개선으로 호스트 실행 브리지(`codex-execute-host-job.sh`), 실행 감사 JSONL, no-op·이벤트 경로 검증을 추가했다.
- 현재 판정은 “호스트 브리지 구현 완료, 실제 provider 성공 표본은 아직 미확보”다. 다음 파일럿에서 성공 여부를 확정한다.

## 8. 다음 작업 목록

1. **P0·보류:** Claude 5시간 창 초기화 후 새 작업 ID와 이벤트 파일로 호스트 브리지 실 provider 파일럿 재실행.
2. **P0:** Codex 감사 로그, 실제 diff, nano 이벤트의 `passed` 상태가 서로 일치하는지 확인.
3. **P1:** 실제 provider 성공 표본을 누적해 nano 임계값 분석 조건(이벤트 20건·위험 입력 10건·잔량 신호 10건)을 충족.
4. **P1:** 전체 테스트와 launchd·Telegram·Discord 서비스 health 회귀 확인.
5. **후순위:** 병렬 worktree 실행을 위한 전역 writer·merge·rollback 계약 설계.
6. **후순위:** Team OS 어댑터 검토. 엣지 에이전트 안정화 전에는 연결하지 않고 독립 운영 유지.

## 9. 2번 점검 결과 (2026-07-31)

- Codex 감사 원장에는 provider 프로세스가 `exitCode=0`으로 종료된 기록이 있으나, 메시지 본문에는 `sandbox_apply: Operation not permitted`와 실제 수정 실패가 포함되어 있었다.
- 따라서 `provider_completed`는 “프로세스 종료”일 뿐 “작업 성공”을 뜻하지 않는다.
- nano 이벤트 4건은 모두 `status=failed`, `nano_light_blocked`였고, 실제 `pilot.py` 변경 없음 및 완료조건 미충족 판정과 일치했다.
- 감사 원장 redaction·구조 계약 테스트는 통과했다.
- 결론: 2번은 완료했지만, 호스트 브리지가 실제로 작업을 성공시키는지는 1번 파일럿에서만 확정할 수 있다.

## 10. 3번 사전 분석 결과 (2026-07-31)

- 분석기 실행과 테스트는 정상 통과했다.
- 현재 파일럿 이벤트는 파일별 1건이며 모두 `failed`; `passed` 성공 표본은 0건이다.
- 위험 입력 신호는 1건, provider/token 잔량 신호는 0건이다.
- 임계값 조정 자격은 `false`이며 현재 기준값(누적 파일 3 초과·스텝 파일 3 초과·잔량 10% 이하)은 변경하지 않았다.
- 결론: 3번의 분석 준비는 완료했지만, 성공 표본 누적은 호스트 브리지 파일럿 성공 후 진행한다.

## 11. 4번 회귀·서비스 점검 결과 (2026-07-31)

- Python 테스트: 46개 전부 통과.
- Node 테스트: 57개 중 56개 통과, 1개 실패.
- 실패 항목은 `tests/nano-gate-source.test.js`의 나노 실행 순서 검사이며, 현재 구현이 bounded retry를 위해 `const execution`에서 `let execution`으로 바뀐 것을 테스트가 반영하지 못한 드리프트로 확인했다.
- launchd `print` 기준 Telegram Claude·Codex·Antigravity·Roda 및 Discord·Codex 봇은 모두 `state=running`이고, 프로세스도 현재 확인됐다.
- 로그상 Roda는 `gemma4:latest`에 연결됐고 Discord Gateway도 연결됐다. Telegram Codex의 과거 shared workspace 거부 기록은 있으나 현재 실행 경로는 격리 worktree로 설정되어 있다.
- 결론: 서비스 health는 현재 정상, 회귀 점검은 테스트 1건 수정 후 재실행이 필요하다. 서비스 재시작이나 설정 변경은 하지 않았다.

## 12. 5번 병렬 Worktree 설계 결과 (2026-07-31)

- 실제 병렬 실행은 활성화하지 않고, worktree lifecycle·canonical lock·충돌 예약·전역 writer·merge·rollback·orphan recovery 계약을 문서화했다.
- 서로 다른 worktree라도 파일 선언·의존성·전역 상태가 겹치면 병렬 실행을 거부하도록 정의했다.
- dirty 기준 checkout에서는 자동 merge·stash·reset을 금지하고 `integration_blocked`로 멈추도록 했다.
- 수용 테스트는 fake provider 기반으로 먼저 검증한 뒤에만 실제 provider pilot로 넘어가도록 했다.
- 설계 문서: `docs/edge-agent-parallel-worktree-contract.md`
- 결론: 5번 설계 단계 완료. 구현·병렬 실행·자동 merge는 별도 승인 후 진행한다.

## 13. 6번 Team OS 어댑터 검토 결과 (2026-07-31)

- Team OS의 실제 `dispatch.py`, 역할 계약, 승인 패킷, 증거·실행 결과 모델을 확인했다.
- Team OS는 상위 목표·역할 배정·사람 승인에서 더 구체적이고, 엣지 에이전트는 provider 실행·sandbox·락·usage gate에서 더 구체적이다.
- 라우팅·승인·상태 원장·Roda 역할은 중복 또는 충돌 가능성이 있어 직접 결합하지 않는다.
- Team OS는 상위 요청·승인을 소유하고 엣지는 제한된 로컬 실행·검증 결과만 반환하는 단방향 DTO 어댑터를 향후 검토한다.
- 연결 전제(호스트 브리지 성공, Node 회귀 1건 수정, 승인 owner 확정, token-free contract test)를 정의했다.
- 검토 문서: `docs/edge-agent-team-os-adapter-review.md`
- 결론: 6번 검토 완료. Team OS와 엣지 에이전트는 계속 독립 운영하며, 어댑터 구현은 보류한다.

## 14. 5개 파이프라인 초기 구현 결과 (2026-07-31)

- **P1 Worktree lifecycle:** 명시적 clean base commit, 고유 task ID, detached worktree, atomic manifest, 안전한 제거 구현.
- **P2 Lock·reservation:** repository lifecycle/integration/task lock과 파일·dependency reservation 구현.
- **P3 Provider execution:** provider-neutral fake callback, 실제 diff·undeclared file·diff check·no-op 판정, event handoff DTO 구현.
- **P4 Merge·rollback:** opt-in 단일 merge, dirty target 차단, merge-ready 상태, dirty worktree 보존 rollback 구현.
- **P5 Safety tests:** fake provider 계약 테스트 10개 추가. 비충돌 파일을 선언한
  두 작업이 실제로 동시에 provider 구간을 통과하는 계약도 확인했다.
- 파일 예약 원장의 짧은 임계구역은 비충돌 동시 예약을 `busy`로 오판하지 않도록
  원장 갱신 동안만 blocking lock을 사용하고, 파일 충돌 판정은 그대로 유지한다.
- 검증: Python 56개 전부 통과, Node 57개 전부 통과.
- 실제 provider 병렬 실행, 기존 Telegram·Discord 라우팅 연결, 자동 merge, launchd 변경은 수행하지 않았다. `parallel_enabled` 기본값도 `false`다.
- 구현 파일: `bin/edge_agent_parallel_worktree.py`, `bin/edge_agent_parallel_locks.py`, `bin/edge_agent_parallel_executor.py`, `bin/edge_agent_parallel_integrator.py`, `tests/test_parallel_pipeline_contract.py`
- 결론: 5개 파이프라인의 안전한 초기 구현과 token-free 검증은 완료했지만, 운영 연결 전 단계다.

## 16. 자동 병합 경로 (2026-07-31)

- `bin/edge_agent_parallel_pipeline.py`에 실행 성공 직후 자동 병합하는 명시적
  `ParallelPipeline` 경로를 추가했다.
- 자동 병합은 기본 비활성화이며 `parallel_enabled=True`와
  `automatic_merge=True`를 모두 명시해야 한다.
- provider 실패, 실제 diff 없음, 선언 범위 밖 변경, `diff --check` 실패,
  대상 checkout dirty, merge 충돌은 모두 병합하지 않고 상태와 worktree를 보존한다.
- 통과 시에만 `--no-ff` 병합하고, 기본적으로 깨끗한 worktree를 정리한다.
- 현재 저장소의 기존 파일럿 worktree는 새 매니페스트가 없고 병합 가능한 변경도
  없어 자동 병합하지 않았다. Telegram·Discord·launchd에는 아직 연결하지 않았다.

## 15. 남은 작업 진행 결과 (2026-07-31)

- **동시성 계약 검증 완료:** `test_non_overlapping_tasks_execute_concurrently_when_explicitly_enabled`로
  서로 다른 파일을 예약한 두 fake provider가 동시에 실행되고, 각자의 diff와 이벤트가
  분리되어 기록되는 것을 확인했다.
- **읽기 전용 worktree 감사 추가:** `bin/edge_agent_parallel_audit.py`가 매니페스트와
  `git worktree list`를 대조하여 누락·고아·경로 불일치를 보고한다. 자동 삭제·reset·복구는
  하지 않는다.
- **현재 감사 결과:** `/Users/edge_ai/mac-agent` 기준 병렬 파이프라인 매니페스트와
  Git worktree 불일치 0건.
- **실 provider 파일럿:** 현재 사용량 게이트에서 Claude 5시간 창 잔량이 0%로
  차단되어 실행하지 않았다. 이는 게이트 오작동이 아니라 현재 정책에 따른 정상 차단이다.

### 다음에 남은 작업

1. Claude 사용량 창이 초기화된 뒤 호스트 브리지 실 provider 파일럿 1건 실행.
2. 성공 표본과 token/risk 신호를 누적하여 nano 임계값 조정 자격 재평가.
3. 실제 운영 연결 전 Telegram·Discord 라우팅, 자동 merge, launchd 변경을 별도 승인으로 검토.
4. Team OS 어댑터는 계속 보류하고, 엣지 에이전트 독립 운영을 유지.

## 17. 2026-08-02 후속 검증

- 전용 clean worktree에서 Codex read-only canary를 실제 실행했다.
- 결과는 `returncode=0`, timeout 없음, 변경 파일 0개, `git diff --check` 통과였다.
- 원문 출력은 저장하지 않고 출력 해시·바이트 수만 계산했으며, 효율성 원장에
  멱등 이벤트를 기록했다.
- 병렬 worktree 감사 결과는 finding 0건이며, 실제 병렬 Provider·자동 merge는
  계속 비활성 상태다.
- Nano 이벤트 원장은 현재 0건이므로 임계값 변경 자격은 여전히 `false`다.
- 당시 검증 시점에는 Claude/dual이 5시간 잔여 4%의 알려진 `SKIP` 상태라
  호출하지 않았다. 이후 사용량 회복 및 `coach` 수정 후 Claude canary를 별도
  실행했으며, 최신 결과는 아래 18절에 기록한다.

## 18. 2026-08-02 Claude 사용량 복구 후속 검증

- `coach`의 `reset_min=null` 직렬화 오류를 수정하고 null-reset 회귀 테스트를
  통과시켰다.
- Claude 사용량 게이트는 5시간 100%·7일 91%로 정상 `PROCEED`를 반환했다.
- 전용 clean worktree에서 Claude read-only canary를 실행해 종료코드 0,
  변경 파일 0개, timeout 없음, `git diff --check` 통과를 확인했다.
- 관련 개선 task 2건은 재검증 증거와 함께 완료 처리했다.
- 현재 실제 남은 운영 작업은 nano 이벤트 표본 누적과 병렬/자동 merge의 별도 승인이다.
