# Antigravity 독립 검토 결과

대상: `multi-agent-collaborative-orchestration-work-order-2026-08-02.md`
검토자: Antigravity
판정: changes_required

## 차단 이슈

1. 4개 에이전트가 동시에 Telegram으로 응답할 때 중앙 egress queue, rate limit,
   순서 보장, backpressure가 없다.
2. `agent_message`가 Telegram ingress로 재유입될 때 bot filter와 내부 bus 경계가
   명확하지 않아 무한 재응답 루프가 발생할 수 있다.
3. deliberation 중 외부 실행·파일 변경 등 승인이 필요한 작업이 생겼을 때 전체
   작업 그래프를 멈추고 특정 대기 에이전트로 승인 결과를 되돌리는 계약이 없다.

## 누락 요구사항

- 재시작 후 deliberation graph, subtask, deduplication key, context를 복구하는
  영속 저장소와 lock 계약
- `/cancel`, `stop`, `그만` 등의 전역 취소 프로토콜과 하위 작업 cascade 취소
- Claude가 참여하지 않은 충돌 상황의 arbitration/tie-break 규칙
- 내부 agent_message도 권한 상승이나 승인 우회를 할 수 없다는 경계

## 아키텍처·안전 위험

- subagent tree depth와 동시 실행 수 제한이 없으면 프로세스가 기하급수적으로
  증가할 수 있다.
- mac-agent와 engine-repo의 다중 LaunchAgent가 단일 writer IPC/lock 없이 상태를
  갱신하면 race condition이 생긴다.
- 분산된 직접 fan-out보다 `Ingress → Router → Queue → Orchestrator → Egress`인
  canonical event bus가 필요하다.
- 검색·스크랩·API 자료의 prompt injection이 agent_message로 전파될 수 있다.
- evidence_refs와 로그에 secret이 포함될 수 있으므로 Telegram 전송 전 sanitization
  gate가 필요하다.
- capability discovery는 안전한 read-only allowlist와 approval gate를 거쳐야 한다.

## 토큰·테스트 위험

- 단순 인사까지 4회 모델 호출하면 비용이 4배가 된다.
- 전체 deliberation transcript를 매 라운드 재전달하면 context가 O(N²)로 커진다.
- 캐시 만료·의미적 invalidation 기준이 없다.
- Telegram burst/backpressure, circular loop circuit breaker, prompt injection 격리,
  crash 중간 재시작 복구 테스트가 없다.

## 권고 변경

1. 단순 인사는 단일 응답 또는 통합 응답으로 처리하고, `각자/모두/얘들아`나
   복잡한 deliberation에서만 4인 fan-out을 활성화하는 정책을 검토한다.
2. canonical event bus와 중앙 egress queue를 둔다.
3. agent_message에도 원 사용자 provenance와 전체 approval/risk 검사를 유지한다.
4. 최대 subagent depth, deliberation round, 동시 작업 수, 전체 timeout을 명시한다.
5. evidence·로그·Telegram 출력 전 secret sanitization을 의무화한다.

## 검토 실행 근거

- 작업지시서와 현재 저장소의 관련 문서를 읽고 독립 검토했다.
- 코드와 문서를 수정하지 않았다.

## 최종 구현 재검토 (2026-08-02)

- 판정: `changes_required`
- 확인된 구현: 공통 ingress 라우팅, 부분 지정, durable deliberation barrier,
  signed agent message, shared dedup, canonical Codex owner.
- 남은 차단/공백: 중앙 egress queue·rate limit·backpressure, claim/lease provenance
  검증, approval/cancel 전파, 실제 다중 프로세스 end-to-end와 Telegram canary.
- 주의: 기존 `PEER_ROLES` 3역할은 명시적 peer 상태 보고 경로이고, 4역할 숙의는
  별도 `DeliberationStore` 경로로 구현되어 있으므로 두 경로를 동일한 barrier로
  해석하지 않아야 한다.

## 지적사항 반영 후 상태

- 역할 alias 정규화(`gemma` → `roda`)를 추가했다.
- egress 전역 lock을 채팅별 lock으로 분리하고, async acquire/release를 thread로
  이동해 다른 채팅의 전송과 이벤트 루프를 막지 않도록 수정했다.
- 위 수정 후 관련 테스트와 전체 테스트를 재실행했다. 최종 독립 `pass` 재확인은
  아직 완료하지 않았으므로 본 문서의 전체 판정은 `changes_required`로 유지한다.

## 제어면·복구 라운드 확인 (2026-08-02)

- 승인 요청·승인/거부·전역 취소를 durable control-plane에 연결했고, control
  action은 HMAC 서명·key id·source event id를 검증한다.
- 네 canonical provider 경로와 Codex engine이 task start/completed/failed 및
  cancel cascade를 기록한다. engine 재시작 시 recoverable candidate 수를 로그로
  확인한다.
- shared claim DB의 owner allowlist와 role-scoped claim key를 적용해 broadcast
  fan-out과 동일 역할 중복 제거를 동시에 보장한다.
- `mac-agent 467`, `engine-repo 43` 전체 테스트와 compileall/plist lint를
  통과했으며, 네 canonical 서비스 재기동 후 running 및 full group intake를
  실측했다.

본 기록은 구현 및 로컬 검증 결과이며, 실제 Telegram canary·429 통합 테스트와
claim/lease 자체의 암호학적 provenance는 아직 검증하지 않았다. 따라서 기존
`changes_required` 판정은 최신 독립 재검토가 완료될 때까지 유지한다.

## 최신 재검토 시도 (2026-08-02)

현재 코드 기준 읽기 전용 재검토를 headless Antigravity CLI로 시도했으나,
CLI가 필요한 `command` 권한을 실행 환경에서 자동 거부하여 결과를 반환하지
못했다. 이 시도는 검토 판정으로 기록하지 않으며, 기존 `changes_required`를
유지한다.

## 지적사항 처리 후 증거 (2026-08-02)

- 이전 429 지적을 반영해 canonical engine과 direct Claude·Antigravity·Roda에
  Telegram `RetryAfter` 기반 bounded dynamic backoff를 연결했다.
- claim/lease fencing lifecycle HMAC provenance 및 검증 테스트를 추가했다.
- mac-agent `475 tests`, engine-repo `44 tests` 전체 통과 후 네 서비스 재기동을
  완료했다. canonical Telegram canary `canonical-canary-20260802-r2`는
  `succeeded`, message id `285`로 확인됐다.

이 기록은 이전 reviewer의 `changes_required`를 대체하지 않는다. 이 증거를 기준으로
최신 독립 재검토를 다시 수행해야 한다.

## 공식 독립 최종 재검토 (2026-08-02)

- 판정: `pass`
- blocking_issues: `none`
- missing_requirements: `none`
- 근거: mac-agent `475/475`, engine-repo `44/44` 통과, post-restart canonical
  canary `canonical-canary-20260802-r2` / Telegram `message_id=285`, ingress·signed
  message·shared egress·control-plane·signed claim/lease provenance·canonical
  LaunchAgent·RetryAfter backoff 확인.
- 비차단 위험: legacy Codex LaunchAgent는 rollback 안전성을 위해 disabled 격리
  상태로 보존한다.
