# Claude 독립 검토 결과

대상: `multi-agent-collaborative-orchestration-work-order-2026-08-02.md`
검토자: Claude CLI 2.1.220
판정: `changes_required`

## 범위

작업지시서 전체의 내적 정합성, 구현 가능성, 누락 요구사항, 보안·안전,
토큰·컨텍스트 운영, 테스트 공백을 읽기 전용으로 검토했다. 코드는 열람하지
않고 문서 자체를 기준으로 판정했다.

## 차단 이슈

1. 상위 coordinator와 Claude peer의 역할이 충돌한다. coordinator가 별도
   논리 계층인지 Claude 모델인지, Claude가 peer 의견을 낸 뒤 통합하는 순서가
   무엇인지 명시해야 한다.
2. 검토자 범위가 불일치한다. Phase 3은 Claude·Antigravity·Codex·Roda 4인
   리뷰를 적었지만, 검토 요청과 완료 기준은 Claude·Antigravity만 특정하지
   않아 게이트 통과 기준이 모호하다.

## 누락 요구사항

- `agent_message.from`을 검증할 발신자 인증·무결성 방법이 없다.
- 기존 `coach-headroom.sh`, `usage-advisor.sh`, `route-dispatch.sh`와
  `verify-task-orchestrator.py` 게이트를 새 협업 흐름이 어떻게 계승하는지 없다.
- “안티와 로다가 논의해”처럼 일부 에이전트만 지정하는 발화의 라우팅 계약과
  테스트가 없다.
- 4개 모델의 고유 정체성·판단·문체가 통합 과정에서 유지되는지 완료 기준과
  테스트가 없다.

## 아키텍처·안전 위험

- 총 동시 작업 수와 task/session별 token budget이 수치화되지 않아 비용 상한을
  재현하기 어렵다.
- prompt injection의 원문 분리·untrusted 표시는 있으나 ingress, 도구 출력,
  저장 시점 중 어느 경계에서 정제하는지 설계에 고정되어 있지 않다.
- 승인 상태도 발신자 위조 메시지가 개입하면 조작될 수 있다.
- 현재 작업 트리의 `edge_agent_ingress.py`, `edge_agent_secure_paths.py`,
  `weather_adapter.py` 등 선행 변경과 Phase 0~2의 관계가 문서에 없다.

## 토큰·테스트 공백

- 인사 시 4개 모델 참여를 유지하면서 짧은 응답 profile과 queue batching으로
  비용을 통제하는 방향은 사용자 요구와 일치하며 이미 문서에 반영되어 있다.
- 추가로 token budget, 동시 작업 수, cache TTL을 설정 가능한 수치로 정의해야 한다.
- 정체성 유지, 발신자 위조, 부분 지정 라우팅, coordinator/peer 순서 충돌,
  budget 초과 차단 회귀 테스트가 필요하다.

## 권고 변경

1. coordinator를 별도 모델이 아닌 논리적 control-plane 역할로 정의하고,
   Claude가 peer로 참여할 때 의견 수집과 통합 단계를 분리한다.
2. 필수 리뷰 게이트를 Claude·Antigravity로 명시하거나 4인 리뷰를 실제로
   요구하도록 Phase 3·완료 기준·검토 요청을 일치시킨다.
3. agent message에 `trust_domain`, `key_id`, `signature`, `source_event_id`를
   추가하고 ingress에서 검증한다.
4. 기존 사용량·라우팅·검증 게이트를 새 오케스트레이터의 선행 정책으로
   명시한다.
5. 부분 지정 라우팅과 persona/identity 유지 acceptance test를 추가한다.
6. 깊이·라운드·동시성·작업별/세션별 token budget·cache TTL의 초기값과 hard cap을
   설정 계약으로 정의한다.
7. 선행 dirty 변경을 baseline checkpoint로 기록하고 본 작업의 Phase 2와
   혼동하지 않도록 한다.

## 실행 근거

- 셸 명령: `/Users/edge_ai/.local/bin/claude -p ... --permission-mode plan`
- 인증 상태: `loggedIn: true`, `authMethod: claude.ai`, `subscriptionType: pro`
- 종료 상태: exit 0
- 최초 실패 원인: `claude` shim이 최신 CLI가 지원하지 않는
  `--append-system-prompt` 인자를 주입했음. shim을 우회한 실제 바이너리로
  검토를 완료했다.

## 최신 구현 재검토 요약 (2026-08-02)

- 판정: `changes_required`
- 확인된 개선: 4개 역할 라우팅과 숙의 barrier, HMAC agent message, coordination
  서명 검증, cross-process dedup, canonical Codex owner, 그룹 수신 권한 진단.
- 남은 차단/공백: claim/lease/lock provenance의 서명된 신원 검증, key rotation,
  중앙 egress queue와 backpressure, 다중 프로세스 loop/cancel/restart E2E.
- 실제 검토는 읽기 전용으로 수행했으며, 최신 완료 재검토는 이 항목들을 이유로
  최종 `pass`를 부여하지 않았다.

## 후속 라운드 참고

- 후속 Claude 재검토 프로세스는 제한 시간 내 최종 답변을 반환하지 않아 판정에
  사용하지 않았다. 직전 완료 검토의 `changes_required` 판정과 Antigravity의
  지적을 기준으로 구현을 진행했고, 최종 독립 재검토는 별도 수행 대상이다.

## 제어면·복구 라운드 구현 기록 (2026-08-02)

- 승인·거부·취소 이벤트를 HMAC-signed durable control-plane으로 구현하고,
  취소 cascade와 재시작 후 recoverable 상태 조회를 네 canonical provider 경로에
  연결했다.
- shared claim DB에는 역할별 owner allowlist와 role-scoped claim key를 적용했다.
  동일 역할 중복은 제거하면서 broadcast fan-out의 네 역할 참여는 유지한다.
- mac-agent `467 tests`, engine-repo `43 tests`가 모두 통과했고, compileall,
  diff check, plist lint, drain-aware 서비스 재기동 및 running/intake 진단을
  완료했다.

이는 구현 증거이지 독립 재검토 `pass`가 아니다. 실제 Telegram canary, API 429
통합 테스트, claim/lease 암호학적 provenance, 최신 Claude 독립 검토는 여전히
미완료이므로 기존 `changes_required`를 유지한다.

## 잔여항목 처리 증거 (2026-08-02)

- canonical engine과 direct Telegram bridge 세 경로에 `RetryAfter`/`retry_after`
  bounded dynamic backoff를 추가했다.
- claim/lease HMAC provenance journal 및 signed session metadata를 구현했고,
  fencing lifecycle 검증 테스트를 추가했다.
- mac-agent `475`, engine-repo `44` 전체 테스트 통과 및 서비스 재기동 후,
  canonical live canary `message_id=285`, delivery `succeeded`를 확인했다.

최신 Claude 독립 검토가 아직 없으므로 이 문서의 기존 `changes_required`는
최신 검토 결과가 나올 때까지 유지한다.

## 공식 독립 최종 게이트 반영 (2026-08-02)

Antigravity 공식 독립 reviewer가 현재 증거 기준 `pass`, blocking issues `none`,
missing requirements `none`을 반환했다. 따라서 기술 구현 게이트는 통과했다.
Claude의 과거 `changes_required` 기록은 당시 시점의 판정으로 보존하며, 최신
Claude 재검토를 받기 전까지 historical verdict를 임의로 덮어쓰지 않는다.

## Claude 공식 독립 최종 재검토 (2026-08-02)

- 판정: `pass`
- blocking_issues: `none`
- 확인 기준: ingress fan-out/직접/부분 지정, signed agent messages, shared
  egress queue, approval/cancel/restart control-plane, signed claim/lease
  provenance, canonical Codex LaunchAgent, canonical/direct RetryAfter backoff.
- 근거: mac-agent 475/475, engine-repo 44/44, 네 canonical 서비스 running,
  post-restart canonical canary `message_id=285` 기록 확인.
- 비차단 위험: dirty worktree의 checkpoint/commit 부재와 장시간 퇴역 관찰은
  이번 구현 게이트 밖의 운영 후속 항목이다.
