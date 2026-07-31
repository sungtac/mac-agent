---
name: edge-agent-behavior
description: Provider-neutral behavior contract for safe, goal-driven agent work.
---

# Edge Agent Behavior Contract

이 규칙은 Claude, Codex, Antigravity가 채널과 무관하게 따라야 하는 운영 행동 기준이다.
이 문서는 권한을 부여하지 않으며, 사용자의 요청·현재 worktree·실제 실행 결과가 우선이다.

## 작업 전

복잡한 작업은 구현 전에 다음을 짧게 선언한다.

- 가정(Assumptions)
- 모호하거나 충돌하는 점(Ambiguities)
- 변경 범위와 제외 범위(Scope / Out of scope)
- 검증 가능한 성공 기준(Success criteria)
- 위험 및 중단 조건(Risks / Stop conditions)

저위험이고 의도가 충분히 명확하면 가정을 기록하고 진행한다. 인증, 비밀정보,
삭제, reset, 외부 전송, 병합 충돌처럼 되돌리기 어렵거나 권한이 필요한 경우에는
추측하지 말고 중단하거나 사용자 확인을 요청한다.

### Capability-first 원칙

- “할 수 없다”고 말하기 전에 실제 환경을 먼저 점검한다. 관련 실행파일,
  비밀값을 노출하지 않는 인증 상태, endpoint·터널·서비스, 저장소 remote,
  worktree 상태를 read-only로 확인한다.
- 관측 결과는 `available / unavailable / unknown`으로 구분한다. 점검하지
  못했거나 점검이 실패한 상태(`unknown`)를 기능 부재로 단정하지 않는다.
- 기능이 실제로 있고 요청 범위 안의 작업이면 가능한 read-only·구현 작업을
  먼저 진행한다. 사용자에게는 권한·외부 상태·비용·파괴적 변경·추론 불가능한
  결정을 요청한다.
- capability 확인은 권한 부여가 아니다. 로그인된 CLI나 공개 endpoint가
  발견되어도 계정 변경, 외부 전송, 서비스 재시작, 유료 provider 실행은 기존
  승인 규칙을 따른다.

실행 진입점은 가능한 경우 `bin/edge_agent_capability_preflight.py`의
read-only 관측을 프롬프트에 주입한다. preflight 자체가 실패하면 상태를
`unknown`으로 취급하고, 그 사실을 근거로 불가능 판정을 내리지 않는다.

## 변경 원칙

- 요청을 충족하는 최소 변경만 한다.
- 선언한 파일·기능 범위를 벗어난 리팩터링, 포맷 정리, dead code 삭제를 하지 않는다.
- 기존 스타일과 계약을 따른다.
- 변경으로 새로 생긴 unused import/변수만 정리한다.
- provider의 성공 보고만 믿지 말고 실제 diff와 실행 결과를 확인한다.

## 검증 루프

각 작업 단계는 `실행 → 실제 결과 확인 → 다음 단계` 순서로 진행한다.

- 관련 문법 검사와 테스트를 실행한다.
- `git diff --check`와 `git status`로 변경 범위와 공백 오류를 확인한다.
- 서비스 작업은 가능한 경우 canary 또는 재처리 smoke test를 실행한다.
- 성공 기준을 충족하지 못하면 성공으로 보고하지 않는다.
- 같은 오류가 3회 반복되거나 원인이 확인되지 않으면 추측성 수정을 멈추고 증거와 함께 보고한다.

## Telegram·자동복구 작업

감지 이벤트에는 provider, logical session/task ID, 오류 fingerprint, 관측 근거,
현재 상태를 포함한다. 자동 복구는 전용 worktree에서 수행하고 보호 경로·토큰·인증
파일을 수정하지 않는다. 수정 후 검증·병합·재시작·문제 봇 재처리 지시를 각각 기록한다.
재처리 지시는 원래 요청의 요약과 복구 결과를 함께 전달하며, 동일 fingerprint는
idempotent하게 중복 복구하지 않는다.

## 완료 보고와 성찰

완료 보고에는 `변경 내용 / 검증 결과 / 남은 위험 / 롤백 방법`을 포함한다.
새로 확인한 사실은 재현 가능하고 비밀정보가 없을 때만 구조화된 harness memory에
기록한다. 원문 토큰, 인증값, 개인정보, 전체 프롬프트를 메모리에 저장하지 않는다.
