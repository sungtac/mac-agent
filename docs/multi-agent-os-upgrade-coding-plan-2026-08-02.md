# Multi-Agent OS 격상 코딩 계획 및 실행 기록

작성일: 2026-08-02
조사: Antigravity 독립 조사 보고서(`multi_agent_os_upgrading_investigation_report.md`)
실행: Codex

## 결론

현재 시스템의 평가 결과가 부족한 가장 큰 원인은 모델의 수가 부족해서가 아니라, 모델 사이의 **실행 가능한 협업 상태**가 없기 때문이다. 기존 `DeliberationStore`는 결과를 모아 다음 프롬프트에 주입하는 scatter-gather barrier였고, `AgentMessage`는 실제 inbox/transport 없이 결과 JSON 안에 저장되는 서명 envelope였다. 또한 worktree 수명주기 메타데이터가 worktree 내부에 기록되어 실행 결과가 Git dirty 상태로 남을 수 있었다.

이번 변경의 P0 범위는 다음과 같다.

1. durable inter-agent message bus: 서명 검증, 역할별 수신 대상, durable queue, lease/ack, 중복 제거, bounded round/hop.
2. task graph: root/child task, dependency, ready/blocked/running/completed 상태, 취소 시 cascade.
3. DeliberationStore 연결: 기존 결과 JSON은 호환 projection으로 유지하되 peer message bus를 실제 기록 경로로 추가.
4. worktree hygiene: `.edge-agent-task.json`을 외부 private state로 신규 기록하고, 기존 in-tree metadata는 호환 읽기만 허용.
5. 테스트: 프로세스 간 재생성에 준하는 새 bus 인스턴스의 claim/ack, duplicate suppression, DAG dependency, delegation budget 검증.

## 영역별 조사 결과와 적용

| 영역 | 확인된 부족 원인 | 이번 적용 | 다음 단계 |
| --- | --- | --- | --- |
| peer conversation/delegation | barrier와 결과 envelope만 있고 dispatch/inbox/DAG가 없음 | `edge_agent_message_bus.py`, DeliberationStore bus 연결 | provider worker가 claim한 메시지로 후속 턴을 실행하는 dispatcher |
| Telegram/terminal parity | adapter와 직접 bot에 실행 경로가 분리됨 | bus와 state 계약을 공통 모듈로 유지 | `CanonicalInput/OutputEnvelope` 단일 core로 추가 수렴 |
| resilience/lifecycle | in-flight 복구 및 worktree 수거 기준이 약함 | durable bus journal, lease expiry, cancel cascade, 외부 metadata | SQLite/WAL 또는 동등한 event journal, crash E2E, dirty archive GC |
| identity/provenance | 현재 HMAC key id는 무결성은 제공하지만 agent별 비대칭 identity는 아님 | 기존 HMAC 경계를 bus에서도 강제 | Ed25519 agent identity와 scoped capability token |
| token/budget | 문자열 bound와 사후 사용량 집계 중심 | bus round/message/task budget 강제 | 모델별 tokenizer, session cost circuit breaker |
| observability/docs | 분산 인과관계가 파일 snapshot으로 흩어짐 | bus event journal과 task lineage | trace/span 전파와 문서 정합성 검사 |

## 실행 순서

### P0-A — 메시지 및 작업 그래프

- [x] `MessageBus` durable session state
- [x] signed publish/verify
- [x] per-role claim, lease expiry, acknowledgement
- [x] duplicate suppression and bounded message/task budgets
- [x] parent/child task graph and dependency readiness
- [x] cancellation cascade
- [x] bounded delegation helper
- [x] DeliberationStore integration and transcript rendering
- [x] bounded message dispatcher with handler callback, retry/requeue and ack
- [x] two-round peer follow-up in Claude/Antigravity/Codex/Roda execution paths

### P0-B — worktree/resilience

- [x] 신규 worktree lifecycle metadata를 외부 private state로 기록
- [x] 기존 in-tree metadata fallback 유지
- [x] dirty terminal worktree audit archive 후 reclaim command (`--archive-dirty` 명시 필요)
- [x] durable dispatch checkpoints and recoverable checkpoint query
- [ ] crash/restart checkpoint replay E2E with process termination

### P0-C — canonical parity

- [ ] terminal과 Telegram이 동일한 `CanonicalInputEnvelope`를 사용
- [ ] provider 실행/approval/control-plane을 하나의 core service로 수렴
- [ ] 동일 입력에 대한 routing, authorization, artifact parity fixture

### P1/P2

- [ ] per-agent Ed25519 identity 및 scoped capability token
- [ ] exact tokenizer/context compression/cost circuit breaker
- [ ] distributed trace/span propagation
- [ ] living architecture validation 및 역사적 모순 문서 정리

## 완료 기준

이번 P0-A의 완료 기준은 “모델들이 무제한으로 대화한다”가 아니다. bus에 실제로 signed peer message가 저장되고, 다른 프로세스가 lease를 획득해 처리하고, ack 이후 중복 전달되지 않으며, dependency graph와 dispatch checkpoint가 복구되는 것을 테스트로 증명하는 것이다. 현재 provider 경로는 2-round follow-up까지 자동 실행하며, 3턴 이상과 live canary는 별도 검증 범위다.

전체 Multi-Agent OS 완료 기준은 다음을 추가로 요구한다.

- Telegram/terminal parity fixture 통과
- 최소 3턴 peer debate live integration
- 두 개 이상 child task 병렬 실행과 join
- SIGKILL 이후 checkpoint resume
- agent identity 위조 거부
- token/cost hard cap 발동
- 장기 관찰 및 서비스 재시작 후 orphan/dirty resource 0건

이 기준을 충족하기 전에는 시스템을 완성품이라고 보고하지 않는다.
