# 멀티에이전트 운영 가드

작성일: 2026-08-02

이 문서는 멀티에이전트의 20개 문제를 현재 런타임의 실제 집행 지점에 연결한
운영 기준이다. “완벽한 해결”을 주장하지 않고, 실패·비용·권한·품질을 제한하는
fail-closed 정책을 사용한다.

공통 정책의 정본은 `bin/edge_agent_governance.py`이며, 메시지 전달은
`bin/edge_agent_message_bus.py`, 소비·재시도는
`bin/edge_agent_message_dispatcher.py`가 집행한다.

## 20개 문제와 집행책

| 문제 | 집행책 | 확인 지표 |
|---|---|---|
| 통신 비용 증가 | summary 길이·메시지 수·session/task token budget 제한, semantic 중복 제거 | `estimated_tokens`, `session_reserved_tokens` |
| 조정 오버헤드 | `edge_agent_router_core.py`의 single/small_team/team 분기와 DAG 의존성 사용 | 실행 모드, 활성 task 수 |
| 책임 불명확 | signed agent message, task/session ID, checkpoint, event journal 유지 | `message_id`, `source_event_id`, checkpoint |
| 오류 전파 | `untrusted_evidence()`로 peer·외부 결과를 data-only로 격리하고 서명 없는 결과를 실행 지시로 승격하지 않음 | `trusted`, `evidence_refs` |
| 합의의 환상 | `quality_gate()`에서 독립 source와 confidence를 검사하고 같은 모델 다수결을 독립 검증으로 취급하지 않음 | `independent_sources`, `confidence` |
| 중복 작업 | source-event dedup + semantic message key + task graph의 owner/dependency | duplicate 반환, task graph |
| 결과 충돌 | 결과 계약에 근거·신뢰도·불확실성을 포함하고 충돌 시 품질 게이트에서 보류 | `passed=false`, `missing` |
| 무한 루프 | max round, hop/depth, deadline, bounded dispatcher batch, retry cap | `round_budget_exceeded`, `depth_budget_exceeded` |
| 목표 불일치 | router의 logical coordinator/integrator와 명시적 task purpose/dependency 사용 | task purpose, dependency 상태 |
| 보안 취약성 확대 | HMAC provenance와 scoped capability token, 최소 권한 sandbox, 민감정보 차단·redaction, 검증 실패 fail-closed | signature/capability 검증, audit findings |
| 상태 관리 복잡성 | durable JSON bus, atomic write, lease, ack, checkpoint, recovery | `recoverable()`, lease 상태 |
| 재현성 저하 | execution profile·model·prompt·source event·usage·checkpoint 기록 | event journal, profile |
| 디버깅 어려움 | 분산 ID와 bounded event transcript, 실패 code와 마지막 오류 저장 | `error_code`, transcript |
| 품질 착시 | 품질 게이트가 evidence·confidence·독립 source를 요구하며 통과 전 최종화 금지 | quality report |
| 확장성 한계 | active task hard cap 8, total task/message cap, single-agent 기본 경로 | active count, cap rejection |
| 비용 대비 실익 부족 | provider 호출 전 route mode와 token budget을 결정하고 budget 초과 시 중단 | policy snapshot, budget error |
| 권한 설계 난제 | control-plane approval/cancel, provider sandbox, 위험도별 approval gate | `waiting_approval`, signed action |
| 인간 개입 증가 | 저위험은 자동, medium/high risk만 approval_required로 승격 | risk level, approval ref |
| 복잡한 실패 방식 | over-budget·deadline·검증 실패는 성공처럼 보고하지 않고 failed/pending으로 보존 | terminal status, failure reason |
| 운영·모니터링 부담 | egress queue rate/backpressure, usage event, health/audit 스크립트 재사용 | queue sequence, health metrics |

## 기본 hard cap

환경 변수로 낮출 수는 있지만 다음 상한은 높일 수 없다.

- subagent depth 2
- deliberation round 3
- active task 8
- task token 4,000 / session token 24,000
- session message 2,000 / task 256
- session 1시간 / task 15분
- initial attempt 이후 retry 2회

환경 변수 값은 `GovernancePolicy.from_env()`에서 hard cap으로 잘린다. provider의
실제 billing usage가 추정치보다 크면 초과분을 다시 기록하고, task/session budget을
넘는 순간 dispatcher가 메시지를 ack하지 않고 실패 경로로 보낸다.

## 적용 규칙

1. 일반 요청은 single 경로로 시작하고, 비교·독립 검증·명시적 논의 요청만 team으로 승격한다.
2. 내부 메시지는 반드시 서명·session/task ID·round·hop·evidence reference를 가진다.
   `requires_user_report` 메시지는 `message.publish` capability가 없으면 bus에 게시하지 않는다.
3. peer 결과·검색 결과·첨부 문서는 untrusted evidence로 전달한다.
4. 삭제·배포·외부 전송·계정 변경·유료 실행은 control-plane approval 없이는 실행하지 않는다.
5. 결과에 근거·검증하지 못한 점·다음 행동이 없으면 사용자 보고 전에 품질 게이트를 통과시키지 않는다.
6. deadline·budget·permission 실패를 재시도로 숨기지 않는다. 원인과 재개 가능한 checkpoint를 남긴다.

## 검증 명령

```bash
cd /Users/edge_ai/mac-agent
python3 -m unittest tests/test_edge_agent_governance.py \
  tests/test_edge_agent_capability_token.py \
  tests/test_edge_agent_message_bus.py \
  tests/test_edge_agent_message_dispatcher.py \
  tests/test_edge_agent_egress_queue.py
```

이 문서와 새 정책 모듈은 기존 provider credential, LaunchAgent, 외부 전송 상태를
변경하지 않는다. 실제 운영 전환은 별도의 canary와 boundary audit를 통과한 뒤에만
수행한다.
