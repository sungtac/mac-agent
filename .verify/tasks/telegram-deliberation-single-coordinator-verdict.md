# 작업: Telegram deliberation 최종 판정을 진행자(Claude) 단일 발화로 통합

## 문제
`bin/telegram-agent-bot.py`의 deliberation 처리부(대략 2098~2213행)에서
Claude/Codex/Antigravity/Roda 네 개의 독립 프로세스가 동일한 사용자 fan-out
메시지에 대해 각자 1차→2차→3차 라운드를 수행하고, **각 프로세스가 자신의
3차(adjudication) 결과를 텔레그램 그룹에 개별적으로 reply_text로 전송**한다.

실제 운영 로그(2026-08-02 canary, 세션
`/Users/edge_ai/.edge-agent/state/deliberations/message-bus/delib-3f6c91c051690ca5426d09a79bc5cbf4.json`)
에서 확인된 결과:
- 4개의 서로 다른 "최종 판정" 메시지가 사용자에게 개별 전송됨
- 숫자가 서로 불일치 (예: delivery ack 12/12 vs 0/9 vs Codex가 자기 자신의
  2차 서명 유무를 다르게 진술)
- Roda의 3차 응답이 완전히 무관한 일반론(placeholder 표)으로 오염됨
- 사용자 피드백: "이렇게 끝나면 에이전트들끼리의 회의가 아닌거 같다. 지시 후
  회의를 주도하는 주도자가 있고 주도자가 진행시키는 구조가 필요하다"

## 원하는 동작
문서 `docs/multi-agent-collaborative-orchestration-work-order-2026-08-02.md`
4절·11절에 이미 정의된 "Claude = 기본 coordinator, 최종 통합" 역할을 이
runtime 경로에 실제로 적용한다.

1. Codex/Antigravity/Roda (non-coordinator 세 역할):
   - 1~3차 서명된 의견 생성·`DeliberationStore.record()` 기록은 현행 유지.
   - 3차 기록 완료 후 텔레그램에 보내는 메시지를 "최종 판정" 전체 텍스트가
     아니라 짧은 상태 메시지로 교체한다 (예: "🔧 {역할} 3차 의견을 기록했습니다.
     최종 판정은 진행자가 통합해 안내합니다."). 자신의 3차 텍스트를 사용자에게
     독립적으로 "최종 판정"처럼 노출하지 않는다.

2. Claude (coordinator):
   - 자신의 3차 결과를 기록한 뒤, 기존 `_require_deliberation_round(session,
     2)` 패턴과 동일하게 3차 라운드 barrier(전원 3차 완료)를 바운드된 타임아웃
     으로 대기한다.
   - barrier 도달 시: `DeliberationStore().render(session, consumer_role="claude")`
     로 4명의 서명된 3차 증거를 모두 모아 **한 번 더** 종합 provider 호출을
     수행해 하나의 통합 최종 답변을 만들고, 이것만 사용자에게 전송한다.
   - barrier 타임아웃 시: 몇 개 역할의 3차 결과가 아직 없는지 구체적으로
     보고하고, 완료된 것처럼 위장하지 않는다 (기존 "확인하지 않은 완료 상태를
     만들어내지 않는다" 원칙 준수).

## 제약
- 기존 서명(`agent_message.v1`)·durable dedup·message bus 계약은 변경하지
  않는다. 신규 barrier 대기만 추가.
- Claude가 아닌 세 역할의 1~3차 실제 판단 로직(각자 실행하는 provider 호출)은
  건드리지 않는다 — 텔레그램에 노출되는 마지막 메시지 형태만 바꾼다.
- coordinator가 자기 자신의 peer 의견을 독립 reviewer로 재사용하지 않는다는
  기존 원칙(work-order 4절)을 지킨다: 종합 단계는 4명의 증거를 모두 인용하는
  통합이지, Claude 자신의 3차 의견을 그대로 재포장하는 것이 아니다.
- 최소 변경. 관련 없는 리팩터링 금지.

## 완료 조건
- `bin/telegram-agent-bot.py` 변경 후 기존 pytest 전체 통과 (특히
  deliberation/barrier 관련 테스트).
- 4개 역할 fan-out 시나리오에서 텔레그램에 노출되는 "최종 판정" 성격의 메시지가
  1건(coordinator 종합)만 발생하는지 코드 경로로 확인 가능해야 한다.
- barrier 타임아웃 시 부분 결과를 정직하게 보고하는 경로가 테스트로 커버된다.
