# session-cost-gate-stop-check.sh (세션 컨텍스트 임계값 경고)

Stop 훅. 세션의 **현재 컨텍스트 크기**가 180,000 토큰(영상이 제안한 200K auto-compact
기준보다 약간 낮게 잡은 여유값)을 넘으면 세션당 1회 블록해서 새 세션을 고려하라고
안내한다. 근거: 실밸개발자 Claude Code 강의 2편(메타 근무 경험 기반, 2026-07-29 검토·반영)
— 같은 출처에서 나온 `CLAUDE.md`의 "세션 중 CLAUDE.md 수정/모델 전환 금지" 규율,
`token-cost-dashboard` 포터블 스킬과 세트.

## 왜 "누적 사용량"이 아니라 "현재 컨텍스트 크기"인가

이 세션에서 지금까지 쓴 토큰을 다 더하는 게 아니다 — 그러면 매 턴 겹치는 캐시된 내용을
계속 중복 계산하게 된다. 대신 **가장 최근 assistant 턴 하나**의
`input_tokens + cache_read_input_tokens + cache_creation_input_tokens`를 본다. 이게
그 턴 시점에 실제로 프롬프트에 들어가 있던 컨텍스트 크기와 가장 가깝다. (누적 비용·캐시
효율을 여러 세션에 걸쳐 보고 싶으면 `token-cost-dashboard` 스킬을 쓸 것 — 이 훅은 그거랑
목적이 다르다: 훅은 "지금 이 세션 계속 가도 되나"를 실시간으로, 스킬은 "지금까지 어땠나"를
사후에.)

## 동작

- stdin에서 `session_id`/`transcript_path`만 읽는다(`verify-task-stop-check.sh`와 동일 패턴).
- `model=="<synthetic>"`(rate-limit 등 에러 턴)는 스킵.
- 세션당 1회만 블록: `$HOME/.claude/hooks-state/session-cost-gate-nag/${SESSION_ID}.nagged`
  마커, 7일 지나면 자동 정리. 임계값 미만이면 조용히 통과.
- 블록 메시지에 현재 컨텍스트 토큰 수와 마지막 턴의 캐시적중률을 같이 보여주고, "CLAUDE.md
  고치거나 모델 바꿀 계획 있으면 지금이 타이밍"이라고 N1 규율을 다시 상기시킨다.

## 알려진 한계

- 임계값(180,000)은 고정값이다 — 작업 성격에 따라 이보다 낮거나 높은 게 맞을 수 있다.
  필요하면 스크립트 상단 `THRESHOLD` 값을 직접 조정.
- 이 컨텍스트-크기 근사치는 "가장 최근 assistant 턴 하나"만 보므로, 그 턴 자체가
  `<synthetic>`(에러)뿐이었던 세션은 감지가 안 될 수 있다(드문 케이스).
