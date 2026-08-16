# 텔레그램 회의 모드에 사용자 끼어들기 기능 추가

## 배경

`bin/telegram-agent-bot.py`와 `bin/edge_agent_deliberation.py`가 구현한 텔레그램
"회의 모드"(conversation meeting)는 사람이 의견을 묻는 톤의 메시지(`is_conversation_meeting`)를
보내면 Claude/Codex/Antigravity/Roda 4개 에이전트가 `DeliberationStore`를 통해 1~3차
라운드를 거쳐 각자 서명된 의견을 기록하고, 3차 barrier 통과 후 coordinator(Claude)가
네 명의 증거를 모아 한 번 더 종합 호출을 수행해 최종 답변 하나만 그룹에 보낸다
(`docs/multi-agent-collaborative-orchestration-work-order-2026-08-02.md` 4·11절,
`.verify/tasks/telegram-deliberation-single-coordinator-verdict.md`).

이 구조는 "메시지 하나 → 회의 한 번 → 최종 답 하나"로 닫혀 있다. 회의가 진행되는
동안 사용자가 추가 메시지를 보내도 그것을 진행 중인 회의의 발언으로 반영할 경로가
없고, 새로운 독립 회의로 분류될 가능성이 높다. 사용자 피드백: "회의 중간에 끼어들
수 있게 만들자."

## 목표

텔레그램 그룹에서 활성 회의(active `DeliberationStore` 세션)가 진행 중일 때
사용자가 보내는 메시지를, 새 회의를 여는 대신 **진행 중인 회의의 발언 하나**로
반영한다. 라운드 경계를 사용자가 신경 쓸 필요 없이, 어느 시점에 보내든 아직
말하지 않은 에이전트와 coordinator의 최종 종합에 자동으로 반영되어야 한다.

## 비목표

- 진행 중인 provider 호출(이미 시작된 라운드 실행) 자체를 중간에 취소·재시작하지
  않는다. 이미 시작한 발언은 끝까지 마치게 두고, 사람의 메시지는 그다음 아직
  시작하지 않은 발언/최종 종합에 반영한다.
- 라운드 1~3차의 서명(`agent_message.v1`)·barrier·durable dedup 계약은 변경하지
  않는다. 사람 발언은 이 서명 체계와 분리된 별도 스트림으로 다룬다.
- 여러 채팅방에서 동시에 여러 회의가 열리는 경우의 전면적인 멀티 세션 UI는
  다루지 않는다 — 채팅방 1개당 활성 회의 1개라는 기존 가정을 그대로 유지한다.
- Discord/Mattermost 등 다른 채널로의 확장은 다루지 않는다 — 텔레그램 회의
  모드에 한정한다.

## 설계

### 1. 사람 발언 저장: 별도 append-only 이벤트 스트림

`DeliberationStore`의 기존 `record()`(에이전트 라운드 결과, barrier 판정에 쓰임)와
사람 발언을 같은 통에 섞지 않는다. Codex/Antigravity 독립 검토에서 지적된 대로,
섞으면 barrier 판정 로직이 사람 발언을 에이전트 응답으로 오인하거나, 반대로
에이전트 재시도 시 사람 발언이 덮어써질 위험이 있다.

새 메서드 `DeliberationStore.append_human_note(session_id, text, *, telegram_message_id)`를
추가한다:
- 세션별로 단조 증가하는 `seq` 번호를 붙여 저장한다(기존 세션 상태 파일에
  `human_notes: [{seq, text, telegram_message_id, recorded_at}]` 배열로 추가).
- 저장 즉시 텔레그램에 짧은 확인 응답을 보낸다(예: "💬 다음 회의 발언에 반영됩니다") —
  Antigravity가 지적한 사용자 피드백 확보.

### 2. 각 에이전트 호출 시점의 반영: `last_seen_seq` 바인딩

각 에이전트가 자기 라운드를 시작하기 직전 `DeliberationStore.render()`를 호출하는
지점에서, 그 시점까지 기록된 `human_notes`를 함께 프롬프트에 포함시킨다. 이때
그 에이전트가 "어느 seq까지 읽었는지"를 해당 라운드 기록(`record()`)에 같이
남긴다(`observed_human_seq` 필드 추가). Codex가 지적한 대로 이렇게 시점을
명시적으로 남겨야, 나중에 각 에이전트 답변이 서로 다른 시점의 회의록을 참조해서
맥락이 어긋나는 문제를 진단할 수 있다.

### 3. 최종 종합 직전 재확인: 상한이 있는 재통합

coordinator가 3차 barrier를 통과해 최종 종합 호출을 만들기 **직전**에, 그 세션의
`human_notes` 중 아직 어떤 에이전트의 `observed_human_seq`에도 반영되지 않은
새 항목이 있는지 확인한다.

- 없으면: 기존과 동일하게 바로 최종 종합.
- 있으면: 그 발언을 포함해 coordinator가 종합 프롬프트를 한 번 다시 만들어
  재종합한다.
- 재종합은 **세션당 최대 1회**로 제한한다(Antigravity가 지적한 "연속 메시지 →
  무한 재종합" 방지). 상한을 넘는 새 발언은 종합 답변 말미에 "추가 의견은 다음
  회의에서 다룹니다" 같은 안내로 처리하고 버리지 않는다(다음 회의 트리거 시
  `human_notes`에 남아있는 미반영 항목을 컨텍스트로 이어받음).

### 4. 활성 회의 판별: 새 메시지가 새 회의인지 발언인지

`handle_message`에서 `is_conversation_meeting(text)`로 분류하기 전에, 같은
채팅방에 진행 중인(barrier 3차 미완료) `DeliberationStore` 세션이 있는지 먼저
확인한다. 있으면:
- 텍스트가 명백한 실행 지시(`_EXECUTION_ACTION` 매치)가 아닌 한, 새 회의를 열지
  않고 `append_human_note`로만 처리한다.
- 명백한 실행 지시면 기존 정책대로 새 요청으로 처리한다(비목표에서 다루는 영역
  밖 — 기존 라우팅 규칙을 그대로 존중).

세션이 없으면(회의가 끝났거나 애초에 없었으면) 기존 로직 그대로 새 회의 판별을
따른다.

## 에러 처리

- `append_human_note` 저장이 실패해도(디스크 오류 등) 사용자의 원래 메시지를
  일반 fan-out 경로로는 처리하지 않는다 — 실패를 텔레그램에 짧게 알리고
  (`"⚠️ 발언 반영에 실패했습니다, 다시 보내주세요"`) 조용히 삼키지 않는다. 기존
  "완료하지 않은 것을 완료로 위장하지 않는다" 원칙을 그대로 따른다.
- 재통합 상한(1회) 도달 후에도 계속 새 발언이 들어오면, 매번 재종합을 시도하지
  않고 위 3절의 안내 메시지만 반복 전송한다 — 조용히 무시하지 않는다.
- `observed_human_seq` 기록이 없는(구버전 상태 파일 등) 세션을 만나면 `seq=0`으로
  간주해 안전하게 하위 호환한다.

## 테스트

`tests/test_edge_agent_deliberation.py`, `tests/test_context_envelope_continuity.py`
patterns를 따라 최소 다음 케이스를 추가한다:
- 활성 회의 중 사람 메시지 → `append_human_note` 호출, 새 독립 회의가 열리지
  않음을 검증.
- 회의 중 없음 상태에서 같은 텍스트 → 기존 `is_conversation_meeting` 분류 로직이
  그대로 새 회의를 여는지 회귀 검증(하위 호환).
- 3차 barrier 통과 직전 새 `human_notes`가 있을 때 coordinator가 1회 재종합하고,
  그 이후 추가 발언에는 재종합하지 않고 안내 메시지만 보내는지 검증.
- `observed_human_seq`가 각 라운드 기록에 정상적으로 남는지 검증.
