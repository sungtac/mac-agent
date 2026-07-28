# route-dispatch.sh + usage-advisor.sh + usage-routing-check.sh (사용량 균형 라우팅)

## 기반 규칙 (사용자 확정 2026-07-26, Rule A 객관화 개정 2026-07-27)

**Rule A — 예외는 "판단"이 아니라 "사실 확인"으로만 인정한다.** 일상적인 대화·작업은 예외 없이 아래 4원칙 라우팅 절차를 탄다. 클로드가 스스로 "이건 내 고유 판단이 필요하다"고 여기는 것만으로는 더 이상 예외 사유가 되지 않는다 — 다음 두 가지, 기계적으로 확인 가능한 경우에만 이 세션이 라우팅 없이 그대로 진행한다:

1. **독립검사 실행 중** — `verify-task`/`verify-task-v2`/`independent-critique-loop` 같은 스킬·워크플로우가 이 세션에서 호출된 경우. 평가자가 작성자와 달라야 한다는 설계상 애초에 클로드 자신에게 위임할 수 없는 일이라 라우팅 대상이 아니다.
2. **코덱스가 물리적으로 못 하는 도구가 필요한 경우** — 예: `mcp__claude-in-chrome` 브라우저 자동화처럼 이 세션에만 연결된 도구를 실제로 사용해야 하는 작업.

단, 클로드 자체 사용량이 `coach` 기준 `red`면(위 두 예외에 해당하더라도) 범위를 최소화한다.

*(구 Rule A는 "오케스트레이터 고유 판단이 필요한 작업"이라는 주관적 문구로, 클로드가 아무 작업에나 갖다 붙일 수 있는 핑계가 됐었다 — 2026-07-27 대시보드 디버깅 세션에서 이 핑계로 실시간 디버깅을 전부 직접 처리한 뒤 자체적으로 지적된 문제. 위 두 항목으로 좁혀서 재발을 막는다.)*

**Rule B — 안티그래비티는 명시적 트리거 시만.** 안티그래비티(gemini 백엔드)는 아무 작업에나 기본값으로 안 쓴다. `route-dispatch.sh` 호출 자체가 그 "명시적 트리거"다 — 즉 이미 "위임 가능하고 단순함"으로 판단된 작업에 한해서만 트리거가 성립한다. Rule A 영역(위 두 예외)에는 애초에 적용되지 않으므로 Rule A와 충돌하지 않는다.

## 4원칙 라우팅 절차

이 순서는 Rule A/B 아래에서 실제로 작업을 배정하는 절차다:

1. **코덱스 가능 작업**: 코덱스와 클로드의 잔여 사용량을 비교해서 여유 있는 쪽에 배정 — `workflows/lib/usage-advisor.sh`가 `coach` 데이터로 결정론적으로 계산.
2. **1번만큼도 아닌 아주 단순한 작업**: 잔여 사용량이 가장 넉넉한 쪽(보통 안티그래비티)에 배정 — `workflows/lib/route-dispatch.sh`. 이게 Rule B의 "명시적 트리거".
3. **안티그래비티도 부족하면**: 클로드·코덱스 중 잔여 사용량이 더 많은 쪽이 처리 — `route-dispatch.sh`가 안티그래비티 실패/고갈 신호를 받으면 코덱스로 폴백(안티그래비티 자체 사용량은 `coach`가 못 봐서 "데이터 부족" — 그래서 먼저 낙관적으로 시도하고 실패를 고갈 신호로 삼는 구조).
4. **Rule A의 두 예외(독립검사 실행 중 / 코덱스가 못 하는 도구 필요)**: 이 순서와 무관하게 클로드가 진행, red면 범위 최소화. 그 외엔 예외 없음 — "고유 판단"을 자체 사유로 든 채 1~3번을 건너뛸 수 없다.

## 코드 강제화의 실제 한계 — 정직하게 밝힘

- **2·3번은 완전히 강제화됨**: `route-dispatch.sh`가 안티그래비티→코덱스 폴백을 스크립트 안에서 실행. 클로드가 손댈 여지 없음.
- **1번은 "비교 계산"만 강제화됨, "배정 실행"은 강제 불가**: `usage-advisor.sh`가 `coach`(→`coach-headroom.sh`) 데이터로 `PREFER: codex` 또는 `PREFER: claude`를 결정론적으로 출력하지만, 결과가 "claude"면 그건 그냥 "이 세션이 스스로 한다"는 뜻이라 어떤 프로세스도 실행되지 않는다 — 셸 스크립트가 살아있는 오케스트레이터 세션을 대신 움직이게 할 방법이 없다는 구조적 한계. 대신 **비교 자체는 코드가 결정**하므로, 클로드가 "이건 코덱스한테 넘기기 귀찮으니 그냥 내가 한다"는 식으로 편의적으로 판단할 여지를 없앤다.
- **4번(Rule A)은 이제 상당 부분 강제화됨**: 두 예외가 "판단"이 아니라 "사실"로 좁혀졌기 때문에, `usage-routing-check.sh`(Stop 훅)가 트랜스크립트에서 (a) 독립검사 스킬(`verify-task`/`verify-task-v2`/`independent-critique-loop`) 호출 흔적, (b) `mcp__claude-in-chrome__*` 툴 사용 흔적을 grep으로 직접 확인해 둘 다 없으면 나그(nag)한다. 여전히 근사치이긴 하다 — 문자열 매치라 "스킬을 불러왔지만 이 작업엔 실제로 안 썼다"류의 오탐/누락은 남는다(verify-task-stop-check.sh와 같은 성격의 한계). 그래도 "그냥 그렇게 여겨서"라는 완전 주관적 핑계는 더 이상 통과되지 않는다.

## 구성 요소

- `workflows/lib/coach-headroom.sh` — `coach --json`에서 클로드 5시간창/코덱스 7일창 잔여율을 `"<claude_pct> <codex_pct>"` 한 줄로 뽑는 공용 헬퍼. `route-dispatch.sh`·`usage-advisor.sh` 둘 다 이걸 재사용(중복 제거). 안티그래비티는 여기서 안 다룸 — `coach`가 애초에 안티그래비티 사용량을 못 봄.
- `workflows/lib/usage-advisor.sh` — Rule 1의 결정론적 비교. 동률이거나 클로드 값을 못 읽으면(0) 코덱스 우선 — 코덱스는 실제로 사용량이 보이고 대체로 여유(96%+ 관찰됨)라, 불확실한 클로드 쪽보다 안전한 기본값.
- `workflows/lib/route-dispatch.sh` — Rule B 트리거 지점. 안티그래비티 우선 시도(`score-dispatch.sh` 위에 얹지 않고 독립 구현 — score-dispatch.sh는 JSON 채점용이라 일반 텍스트 응답을 "파싱 실패"로 오판해서 항상 코덱스로 폴백하는 버그가 실제로 났었음, 테스트 중 발견·수정), 실패/rate-limit 신호 시 코덱스 폴백.
- `hooks/usage-routing-check.sh` — Stop 훅. 위 "코드 강제화의 실제 한계" 참고.

## `usage-preflight-gate.sh` — 예약/트리거 자동화용 사전 게이트 (2026-07-28)

위 넷은 전부 "**라이브 세션 안에서** 이번 작업을 어디로 보낼까"를 다룬다 — `weekly-report.sh`/
`kakao-morning-briefing.sh`(launchd 예약 실행)나 `discord-bot.py`의 Discord 트리거 디스패치처럼
**사람이 지켜보지 않는 상태에서 알아서 시작하는 자동화**가 지금 당장 사용량이 바닥인 채로
그냥 실행돼버리는 문제는 아무도 안 다뤘다. 2026-07-28 하루에만 이 때문에 서로 무관한
자동화(주간보고서·카카오 브리핑·verify-task-v2 전체 트랙)가 최소 8번 독립적으로 세션 한도에
걸려 실패했고, 그중 일부는 10~30분을 돌다가 실패했다 — 예약 작업은 라이브 세션과 달리 도중에
멈춘 걸 알아챌 사람이 없다.

- `workflows/lib/usage-preflight-gate.sh <claude|codex|dual>` — `coach --json`을 다시 읽어서
  (2·3번처럼 `coach-headroom.sh`의 정수 두 개짜리 요약이 아니라 `level`/`reason`까지 그대로
  써야 해서 별도 파싱), 해당 액터가 의존하는 창(클로드는 5시간창 — 이게 오늘 실제 장애를 낸
  창이고 7일창은 널널해도 5시간창만 0%일 수 있음 실측 확인됨; 코덱스는 7일창)이 10% 미만이거나
  `coach`가 그 provider를 `red`로 판정하면 `SKIP: <사유>`, 아니면 `PROCEED`를 stdout 한 줄로
  출력. 항상 exit 0 — 판정은 stdout에만 있다(`route-dispatch.sh`의 `ROUTED-TO: ...` 관례와 통일).
  `coach` 자체가 없거나 에러거나 파싱 안 되면 **무조건 PROCEED**(fail-open) — 체크 도구
  고장이 이 머신의 모든 예약 자동화를 조용히 죽이는 스위치가 되면 안 되므로.
- **모든 caller에 연결 완료(2026-07-28)**: 처음엔 코어 게이트만 만들고(만드는 시점 자체가
  실제로 클로드 5시간창 0%라 검증엔 최적의 타이밍이었음) 연동은 의도적으로 미뤘다 — 그
  상태에서 4~5개 지점 연동까지 한 번에 밀어붙이는 건 게이트가 강제하려는 원칙("사용량 낮으면
  범위를 줄여라")과 스스로 어긋나는 일이라서. 이후 재확인: **코딩(로컬 파일 편집) 자체는
  외부 CLI를 안 부르니 사용량과 무관하게 안전하고, 위험한 건 라이브 전체 실행 검증뿐**이라는
  점에서 범위를 다시 나눴다 — 연동 코드는 전부 지금 작성하고, 실제 launchd/Discord 트리거로
  라이브 전체 실행 검증만 리셋 후로 미루는 절충. 4곳 전부 연결됨:
  `cron/weekly-report.sh`(뮤텍스 락 잡기 전에 확인, SKIP 시 `discord-notify.sh`+pending-job),
  `cron/kakao-morning-briefing.sh`(SKIP 시 일방향 알림만, pending-job 체계 자체가 없음),
  `hooks/work-log-stop-check.sh`(`.dispatched` 마커 세운 뒤 확인, SKIP 시 기존
  `write_pending_job()` 재사용), `discord-bot.py`의 `usage_gate_check()` 공용 헬퍼(세
  곳에서 재사용: `handle_verify_task_v2_retry`/`handle_verify_task_v2_decision_retry`는
  actor=claude, `handle_codex_dispatch`는 actor=codex). 전부 코드 리뷰 + 샌드박스/모킹
  테스트(실제 저사용량 데이터로 SKIP 분기 재현, 알림·pending-job 파일을 스크래치 경로로
  리다이렉트)로 검증 — 실제 launchd 스케줄이나 진짜 Discord 왕복으로 라이브 전체 실행까지는
  아직 안 함(다음 실제 발동 때 확인). 상세 근거는 각 커밋 메시지와 `docs/discord-bot.md`/
  `docs/kakao-playmcp.md` 참고.
- **`usage_gate_check()` 자체에 타임아웃 없던 결함(2026-07-29, `!코덱스` 코드리뷰로 발견)**:
  `discord-bot.py`의 `usage_gate_check()`가 `usage-preflight-gate.sh`(→`coach --json`)를
  기다리는 데 원래 `asyncio.wait_for` 없이 무기한 대기했다. `coach`가 세션 도중 실제로
  자기 provider 조회 중 하나에서 "조회 시간초과(hang?)"를 낸 사례가 이미 관측됐는데, 이
  호출이 `FREE_CHAT_LOCK`/`CODEX_DISPATCH_LOCKS[alias]` **안에서** 일어나서, 진짜로 멈추면
  그 락이 봇을 재시작하기 전까진 영원히 안 풀리는 구조였다. `USAGE_GATE_TIMEOUT_SECONDS`(15초)
  추가, 타임아웃도 fail-open(PROCEED)로 처리. 가짜로 영원히 멈추는 스크립트를 넣어 타임아웃이
  실제로 발동하는 것까지 확인.

verify-task.js/verify-task-v2.js의 역할 고정(안티=스펙+평가, 코덱스=초안+실행)은 이 정책과 무관 — 사용량과 상관없이 그대로 유지(자기평가 방지 설계를 사용량 이유로 깨면 안 됨).
