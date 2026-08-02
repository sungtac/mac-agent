# Provider 톱니바퀴 감사 및 개선 기준선

작성일: 2026-07-30

상태: historical. Discord 자유채팅과 Discord provider fallback은 2026-08-02
퇴역했으며, 아래 구조는 보존용 과거 감사 기록이다. 현재 active 라우팅은 Telegram·
터미널·검증 오케스트레이터 경로만 대상으로 한다.

## 목적

Claude의 세션/사용량 한도로 Discord 자유채팅이 멈추지 않도록 Claude, Antigravity,
Codex가 실제 실행 경로에서 서로 이어지는지 감사하고, 변경 후에도 각 연결부가
검증 가능한 상태인지 기준을 고정한다.

## 현재 실제 구조

```text
Discord 자유채팅
  └─ discord-bot.py / handle_free_chat
       ├─ usage_gate_check(claude)
       ├─ 새 세션이고 Codex가 20%p 이상 여유 있으면 usage-advisor→대체 체인
       ├─ Claude claude -p (--resume 세션)
       ├─ quota 문구면 Claude 10초 후 1회 재시도
       └─ 실패하면 _fallback_to_provider_chain
            ├─ Antigravity agy --mode plan (읽기 전용)
            ├─ 실패하면 usage_gate_check(codex)
            ├─ Codex codex exec -s read-only --skip-git-repo-check
            └─ Codex도 차단/실패하면 종료

별도 단순작업 라우터
  └─ route-dispatch.sh
       ├─ Antigravity agy -p
       └─ 실패/고갈로 판정되면 Codex

코딩 검증 워크플로우
  └─ bin/verify-task-orchestrator.py
       ├─ Claude: 계획 비평/최종 리뷰
       ├─ Antigravity: 계획 비평/최종 리뷰
       └─ Codex: 조정/실행
```

핵심 결론: Discord 자유채팅에 Antigravity→Codex 톱니를 연결했고, 새 Claude 세션의
시작점에서는 `usage-advisor.sh`의 잔여량 비교를 실제 라우팅에 반영한다. Codex가
20%p 이상 앞설 때만 Claude를 아끼고 대체 체인으로 시작하며, 기존 `--resume` 세션은
대화 연속성 때문에 Claude에 고정한다. 실행 중 provider 추적/중단과 직전 대체 응답의
다음 Claude 턴 주입도 유지된다.

## 확인된 구성요소와 계약

| 구성요소 | 현재 역할 | 검증 가능한 사실 | 결손 |
| --- | --- | --- | --- |
| `bin/discord-bot.py` | Discord Claude 자유채팅, 새 세션 잔여량 라우팅 + Claude→Antigravity→Codex 폴백 | 권한은 `FREE_CHAT_USER_ID`, 락은 `FREE_CHAT_LOCK`, 현재 provider 추적, 30분 타임아웃, 20%p 히스테리시스 | Antigravity 잔여량은 측정 불가 |
| `bin/codex-bot.py` | Codex 전용 명령/대화, Codex→Claude 위임 | 별도 프로세스/토큰, Codex 저장소 allowlist | 자유채팅 폴백과 공통 provider 실행기가 아님 |
| `bin/discord_bot_common.py` | 공통 환경, 게이트, provider 실행/결과/맥락 계약 | PATH 보정, 게이트 15초 타임아웃, process group 종료, lifecycle callback | 실제 CLI 조합은 라이브 미검증 |
| `workflows/lib/usage-preflight-gate.sh` | Claude 5h/Codex 7d 사전 차단 | 임계값 10%, 게이트 고장 시 fail-open | Antigravity 수치 게이트 불가 |
| `workflows/lib/route-dispatch.sh` | 단순작업 Antigravity→Codex | 짧은 quota 오류/빈 응답만 Antigravity 고갈로 판정 | 자유채팅과 실행/타임아웃 계약 불일치 |
| `workflows/lib/usage-advisor.sh` | Claude/Codex 잔여량 비교 및 새 자유채팅 세션 라우팅 입력 | `coach` 기반 결정론적 추천, 조회 실패 시 fail-open | Antigravity 비교 불가 |
| `bin/verify-task-orchestrator.py` | 코딩 전후 다자 검증 | Claude+Antigravity 비평/리뷰, Codex 실행 | 자유채팅 응답 폴백과 역할이 다름 |
| Stop 훅 | Claude 세션 규율/검증 강제 | verify-task-v2, 라우팅, 컨텍스트 크기 경고 | Discord 헤드리스 출력에는 일부 경고가 안 보임 |

## 실측 기준선

- `bash workflows/lib/usage-preflight-gate.test.sh`: fixture 20건 전부 통과.
- Python 문법 검사: `discord_bot_common.py`, `discord-bot.py`, `codex-bot.py` 통과.
- 셸 문법 검사: `workflows/lib/*.sh`, `hooks/*.sh`, `cron/*.sh`, `bin/*.sh` 통과.
- 실행 파일: Claude, Codex 0.146.0, Antigravity, coach 모두 존재.
- 현재 `coach --json --providers claude,codex,antigravity`는 Claude만 정상 조회하고,
  Codex는 로컬 상태 런타임 초기화 실패, Antigravity는 프로세스 실행 권한 오류를
  반환했다. 이는 사용량 0이라는 뜻이 아니라 “수치 미확인”이다.
- Claude 사전 게이트는 현재 5시간창 78%, 7일창 66%, 전체상태 green으로
  `PROCEED`를 반환했다.

## 톱니바퀴 계약

1. 사전 게이트가 정상적으로 낮다고 판정한 provider는 실행하지 않는다.
2. 사용량 데이터가 없는 Antigravity는 낙관적으로 한 번 시도하되, 빈 응답/비정상
   종료/짧은 quota 오류를 고갈로 판정하고 다음 톱니로 넘긴다.
3. 한 provider의 실패가 다음 provider의 실행을 막지 않는다.
4. 대체 provider로 넘어가도 원래 사용자의 메시지, 채널 맥락, 권한 범위를 유지한다.
5. 같은 요청을 여러 provider가 동시에 실행하지 않는다(`FREE_CHAT_LOCK`).
6. 최종 실패는 “어느 provider가 어떤 이유로 실패했는지”를 사용자에게 보여준다.
7. 실제 파일을 쓰는 코딩 요청을 검증 워크플로우로 우회하지 않고, 자유채팅 폴백은
   응답 제공 경로로만 취급한다. 코딩 검증은 기존 `verify-task-v2` 계약을 따른다.
8. provider 실행 파일/타임아웃/분류 규칙은 테스트에서 fake executable로 재현 가능해야 한다.

## 목표 구조

```text
Claude 사전 게이트/실행/짧은 재시도
  └─ 실패 시 Antigravity 시도(수치 게이트 없음, 결과 health 판정)
       └─ 실패 시 Codex 사전 게이트/실행
            └─ 실패 시 provider별 실패 요약 + 재전송 안내
```

이 구조는 Claude와 Codex 사용량을 직접 비교해 강제로 반반 쓰는 방식이 아니다.
Claude가 정상일 때는 기존 세션 연속성을 보존하고, Claude가 막힌 요청만 Antigravity를
먼저 사용해 Codex의 7일창을 아끼며, Antigravity까지 실패할 때만 Codex를 쓴다.
Antigravity의 quota 가시성이 생기면 이 낙관적 시도를 별도 게이트로 교체한다.

폴백 provider는 `plan`/`read-only`로 실행한다. 따라서 자유채팅 폴백은 응답·조사·변경안
제공 경로이고, 실제 파일 수정은 Claude 복귀 후 기존 `verify-task-v2` 또는 명시적인
Codex 작업 경로에서 수행한다.

## 나노 작업과 통과 조건

| 순서 | 나노 작업 | 완료 조건 |
| --- | --- | --- |
| N0 | 이 감사 문서와 기준선 고정 | 문서 추가 후 diff/링크/문법 이상 없음 |
| N1 | 공통 provider 결과 분류 계약 추출 | quota/비정상/빈 응답 판정이 한 함수와 단위 테스트로 고정 |
| N2 | Antigravity 폴백 실행기 추가 | fake 성공/실패/타임아웃에서 process group과 결과가 정확히 정리됨 |
| N3 | 자유채팅 체인 연결 | Claude 실패→Antigravity 성공, Antigravity 실패→Codex, 사전 게이트 skip 모두 재현 |
| N4 | 최종 실패 봉투와 사용자 메시지 정리 | provider별 원인 누락 없이 1900자 Discord 제한 안에서 전달 |
| N5 | 문서/운영 검증 | 셸·Python·fixture·통합 fake 테스트 전부 통과, git diff 검토 |

각 단계는 다음 단계로 넘어가기 전에 `git diff --check`, 관련 문법/단위 테스트,
톱니 연결 시나리오 테스트를 모두 통과해야 한다.

## 의도적으로 하지 않는 것

- Antigravity 사용량을 알 수 없는 상태에서 임의 잔여율을 만들어 비교하지 않는다.
- `verify-task-v2`의 고정 역할(Claude/Antigravity 리뷰, Codex 실행)을 자유채팅 폴백
  때문에 바꾸지 않는다.
- 실제 Discord/launchd 라이브 호출을 테스트로 자동화하지 않는다.
- 현재 사용량이 낮다는 이유만으로 실제 provider를 소모하는 probe를 실행하지 않는다.

## 현재 남은 한계

- 자유채팅은 아직 Claude/Codex를 매 턴 적극적으로 균등 배분하지 않는다. Claude 정상
  세션의 연속성을 우선하고, Claude quota 실패 때만 대체 provider를 사용한다.
- 실제 `agy --mode plan`과 launchd 권한 환경의 조합은 CLI 도움말 확인까지만 했고,
  계정 사용량을 소모하는 라이브 Discord 왕복은 실행하지 않았다.
- 따라서 이 변경의 자동 검증 범위는 fake executable, process lifecycle, fallback 순서,
  중단 predicate, bounded context, 기존 fixture까지다. 실제 provider 로그인/네트워크/
  Discord Gateway는 배포 후 수동 확인 대상이다.
