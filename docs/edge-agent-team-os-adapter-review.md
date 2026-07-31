# 엣지 에이전트 · Team OS 어댑터 검토

작성일: 2026-07-31
판정: 독립 운영 유지·어댑터 연결 보류

## 1. 조사 범위

Team OS의 실제 `contracts_v2`, `TeamTask`, `AgentContract`,
`orchestration/dispatch.py`, 승인 패킷·증거·실행 결과 모듈과 엣지 에이전트의
provider dispatch, usage gate, sandbox, canonical lock, nano event store를
비교했다. Team OS 저장소의 코드와 상태는 수정하지 않았다.

## 2. 현재 구현 비교

| 기능 | Team OS | 엣지 에이전트 | 판정 |
|---|---|---|---|
| 상위 작업·역할 배정 | `TeamTask`, `AgentRole`, 실제 `dispatch.py`, primary/reviewer | Claude·Codex·Anti의 채널/실행 라우팅 | Team OS가 상위 분류에 더 적합 |
| 승인 경계 | `ApprovalPacket`, 위험 action, rollback·postcheck·완료 증거 요구 | 보호 경로 차단, provider gate, 실패·검증 fail-closed | Team OS가 사람 승인 계약에서 더 강함 |
| 실제 provider 실행 | OpenClaw/실행 결과 계약 중심, 일부는 dry-run·scaffold | Claude·Codex·Anti CLI 직접 실행, timeout·killpg·host bridge | 엣지가 실제 로컬 실행에 더 구체적 |
| 파일 격리·동시성 | 계약·rollback 설계 중심 | canonical repository lock, worktree 계약, nano 멱등 원장 | 엣지가 현재 실행 안전성에 더 구체적 |
| 증거·완료 판정 | `EvidenceRef`, `ExecutionResult`, 사용자 안전 요약 | 실제 diff, nano status/tier, provider 감사 로그 | 서로 보완 가능하지만 계약 중복 |
| 사용량 라우팅 | `ContextPackage.quota_state` 모델 | coach 기반 preflight·headroom·fallback | 엣지가 운영 데이터와 게이트에서 더 구체적 |
| Roda | 내부 coordinator 역할, 최종 사용자 창구 아님 | Gemma4 Telegram 대화 봇 | 이름과 역할이 충돌할 수 있어 직접 결합 금지 |

## 3. 중복·충돌 지점

1. **라우팅 중복:** Team OS의 role dispatch와 엣지의 provider route-dispatch가
   각각 primary/실행 provider를 결정하면 이중 오케스트레이션이 된다.
2. **승인 중복:** Team OS의 human approval과 엣지의 실행 gate 중 어느 쪽이
   최종 권한인지 불명확하면 한쪽의 승인을 다른 쪽이 우회할 수 있다.
3. **상태 중복:** TeamTask/ExecutionResult와 nano JSONL/work state를 양쪽에서
   갱신하면 상태가 서로 다른 완료를 주장할 수 있다.
4. **workspace 충돌:** 두 시스템이 같은 `.openclaw/workspace`를 보지만 Team OS의
   `team_os/`, `state/`, `sukja_telegram/`은 엣지 provider의 보호 대상이다.
5. **Roda 충돌:** Team OS의 Roda coordinator와 Gemma4 Telegram 봇은 이름만
   같고 책임이 다르므로 하나의 runtime role로 합치면 안 된다.

## 4. 권장 소유권

- **Team OS:** 사용자 목표, 상위 task, 역할 배정, human approval, 최종 사용자용
  요약과 상위 evidence 연결.
- **엣지 에이전트:** 단일 Mac에서 provider 선택 실행, sandbox, worktree/lock,
  nano 단계, 실제 diff, timeout·취소·provider usage 기록.
- **Roda Gemma:** 대화·요약·저비용 안내 전용. Team OS coordinator나 provider
  executor로 자동 승격하지 않는다.
- **최종 권한:** 위험·외부 전송·설정·삭제·계정 전환은 Team OS approval이
  없으면 엣지가 실행하지 않는다. 단, 엣지의 자체 보호 gate도 별도로 통과해야 한다.

## 5. 향후 어댑터 최소 계약

어댑터를 만들 경우 양쪽 내부 모듈을 import하지 않고, 명시적 DTO/JSON 계약만
사용한다.

### Team OS → Edge Agent 요청

- `schema`: `team_os.edge_agent_request.v1`
- `request_id`, `task_id`, `objective`, `source`, `risk_level`
- `allowed_files`, `workspace_or_worktree`, `base_commit`
- `required_outputs`, `completion_gates`, `approval_ref`
- raw token·credential·전체 대화 기록은 포함하지 않음

### Edge Agent → Team OS 결과

- `schema`: `edge_agent.team_os_result.v1`
- `request_id`, `task_id`, `status`
- `changed_files`, `verification_tier`, `event_idempotency_key`
- `provider`, `usage_snapshot_ref`, `evidence_refs`
- `error_code`, `next_action`, `rollback_ref`

`status=passed`는 provider exit code만으로 만들지 않는다. 실제 diff, 검증,
원장 기록이 모두 확인된 경우에만 반환한다.

## 6. 연결 전 필수 조건

1. 엣지 provider 파일럿이 host bridge를 통해 실제 성공해야 한다.
2. Node 회귀 테스트의 stale assertion을 수정하고 전체 테스트를 재통과해야 한다.
3. Team OS와 엣지 중 상위 approval owner를 사용자 결정으로 확정해야 한다.
4. shared workspace 대신 request/result 전용 경로와 보호 경계를 정해야 한다.
5. DTO schema, idempotency, timeout, retry, cancellation, rollback ownership을
   token-free contract test로 먼저 검증해야 한다.
6. 실제 provider 연결은 no-op canary와 사용자 승인 뒤에만 제한적으로 수행한다.

## 7. 최종 판정

현재 Team OS는 상위 역할·승인·증거 모델에서 더 고도화되어 있고, 엣지 에이전트는
실제 로컬 provider 실행·sandbox·락·usage gate에서 더 고도화되어 있다. 어느 한쪽을
다른 쪽으로 대체하는 관계가 아니다.

따라서 지금은 어댑터를 구현하거나 Team OS를 엣지 실행 경로에 연결하지 않는다.
엣지 안정화가 끝난 뒤, Team OS가 요청을 발행하고 엣지가 제한된 실행 결과만
반환하는 단방향 계약부터 별도 프로젝트로 검토한다.
