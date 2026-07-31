# 영상 기반 엣지 에이전트 효율화 병렬 구현 계획

작성일: 2026-07-31
상태: 정책 모듈·통합 어댑터·테스트 완료, 실제 provider 파일럿 전

## 적용 원칙

- Ponytail은 plugin을 바로 설치하지 않고 minimality 원칙만 내부 review skill로 흡수한다.
- Claude/Codex/Anti/Gemma의 native 세션은 합치지 않는다.
- 전체 transcript·토큰·비밀값을 공통 컨텍스트에 저장하지 않는다.
- 자동 코드 삭제·자동 모델 전환·자동 병합은 기본 비활성화한다.
- 모든 병렬 작업은 동일한 clean base commit과 선언 파일 범위를 사용한다.

## 병렬 pipeline

| Pipeline | 책임 | 1차 산출물 |
|---|---|---|
| P1 Context Budget | 컨텍스트·로그 상한, handoff 요약 | `edge_agent_context_budget.py` |
| P2 Minimality | Ponytail 원칙 기반 review-only gate | `edge_agent_minimality.py` |
| P3 Execution Profile | provider/model/reasoning/max-turns 정책 | `edge_agent_execution_profile.py` |
| P4 Skill Policy | 관련 skill만 bounded injection | `edge_agent_skill_policy.py` |
| P5 Efficiency Evidence | A/B 비교용 멱등 효율성 이벤트 | `edge_agent_efficiency_events.py` |

## 통합 경계

- `bin/edge_agent_runtime_adapter.py`가 P1~P5를 하나의 opt-in 경계로 묶는다.
- `EDGE_AGENT_EFFICIENCY_MODE=off`가 기본값이다. `observe`는 원문 실행을 유지하면서
  프로파일을 계산하고, `enforce`만 bounded prompt를 실제 provider 입력으로 만든다.
- 어댑터는 provider를 실행하지 않으며, CLI 옵션은 선언형으로만 반환한다. 실제
  Telegram·Discord·터미널 어댑터가 지원하는 플래그만 선택적으로 매핑해야 한다.
- 효율성 이벤트에는 원문 prompt·출력·토큰·비밀값을 저장하지 않고 길이·시간·검증
  등 집계값만 기록한다.
- 현재 Telegram direct bridge에는 이 어댑터를 opt-in으로 연결했다. `off`와
  `observe`는 기존 prompt와 CLI 인자를 유지하고, `enforce`에서만 bounded prompt와
  Claude 모델/turn, Codex reasoning 옵션을 적용한다. Antigravity의 CLI 옵션은
  버전별 차이를 확인하기 전까지 기존 경로를 유지한다.
- Telegram handler는 `telegram-task` 단위로 작업 ID, 성공/실패, 응답 길이, 소요 시간,
  실제 변경 파일 수를 원장에 기록하도록 연결했다. 기록 실패는 작업 결과를 뒤집지
  않는다. provider별 세부 step과 검증 tier 정밀화는 실제 파일럿에서 확인한다.

P1~P5는 파일 소유권을 겹치지 않게 분리하여 병렬 worktree에서 실행한다.
공통 계약 파일과 Telegram·Discord·launchd는 병렬 단계에서 수정하지 않는다.

## 통합·검증 게이트

1. 각 pipeline token-free 테스트 통과
2. declared files와 실제 diff 일치
3. 민감 경로·Team OS 보호 경로 변경 없음
4. 이벤트 기록과 lease 해제 확인
5. 전체 회귀 테스트 통과
6. 단일 저위험 provider 파일럿
7. 사용자 승인 후 Telegram·Discord 연결
8. 그 다음에만 제한적 3-way 병렬 provider 실행

자동 병합은 최종 운영 승인 전까지 비활성화한다. 실패 worktree와 로그는
자동 삭제하지 않고 보존한다.
