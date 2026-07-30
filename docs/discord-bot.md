# discord-bot.py + discord-notify.sh (일정비서 — Discord 연동)

Phase 1 (사용자 요청, 2026-07-26): 온디맨드 트리거 + 일방향(Mac→Discord) 실패/에스컬레이션 알림. Phase 2 v1(사용자 요청, 2026-07-28): `weekly-report.sh` 실패 알림에 답장하면 재시도. Phase 2.5(사용자 요청, 2026-07-28): `work-log-stop-check.sh` 답장 재시도 + `verify-task-v2.js`의 `needs_clarification`(정보 부족 역질문)과 `needsUserDecision`(최대 라운드 소진) 둘 다 답장 재시도 — 후자는 자유텍스트 3지선다를 그대로 해석하지 않고 재시도 의도 키워드(재시도/retry/다시)만 감지하는 방식으로 해결(사용자 확정, 2026-07-28). Phase 3(사용자 요청, 2026-07-29): 본인(free_chat_user_id) 전용 자유 채팅 — 접두어 없이 전부 릴레이, 전체 도구 허용, `--resume`로 세션 연속성 유지(`!새대화`로 초기화).

## 구성 (2026-07-30 갱신 — 봇 프로세스가 둘로 분리됨)

**2026-07-29에 `!코덱스` 계열 명령이 `discord-bot.py`에서 별도 프로세스 `codex-bot.py`로
분리됐다** — 이 문서 원본(2026-07-29T01:13 마지막 수정) 작성 당시엔 아직 분리 전이라, 아래
"`!코덱스`" 절은 옛 위치를 그대로 서술한 채 남아있었다(2026-07-30 통합 감사로 발견·정정).
현재 정확한 구조:

- `bin/discord-bot.py` — 상시 구동 프로세스(Discord Gateway WebSocket 연결). `~/Library/LaunchAgents/com.macagent.discord-bot.plist`로 launchd 상시 등록(`KeepAlive: true`, `RunAtLoad: true`) — 주간보고서처럼 주기 실행이 아니라 항상 떠 있어야 함. `~/.claude/discord-bot/venv`(격리된 venv, `discord.py 2.7.1`)로 실행. 코드를 고치면 재기동 필요: `launchctl kickstart -k gui/$(id -u)/com.macagent.discord-bot`. `!주간보고서`/`!상태`/`!새대화`/`!중지`, work-log/verify-task-v2 답장 재시도, Phase 3 자유채팅을 담당.
- `bin/codex-bot.py` — **별도 프로세스, 별도 launchd 등록**(`com.macagent.codex-bot.plist`, 같은 `KeepAlive`/`RunAtLoad` 패턴, 재기동은 `launchctl kickstart -k gui/$(id -u)/com.macagent.codex-bot`), **별도 설정 파일**(`~/.claude/discord-bot/codex-bot-config.json` — `discord-bot.py`의 `config.json`과 의도적으로 분리, 자체 토큰). `!코덱스`(직접 디스패치), `!코덱스대화`(연속 대화, 세션 지속), `!코덱스대화초기화`(세션 리셋)를 담당 — 상세는 아래 "`!코덱스` 계열 명령" 절.
- `bin/discord_bot_common.py` — 두 프로세스가 공유하는 헬퍼 모듈: 서브프로세스 env 빌더(`SUBPROCESS_ENV`), 사용량 게이트(`usage_gate_check`), provider 결과/폴백 체인/세션 맥락 계약(`ProviderResult`, `run_provider_attempt`, `run_provider_fallback_chain`, `*_provider_context`), 프로세스그룹 종료 헬퍼(`_kill_process_group`/`_kill_process_group_graceful`), 코덱스 wake-word 상수(`CODEX_CHAT_WAKE_WORDS`).
- **두 프로세스가 같은 Discord 채널을 함께 본다**(`config.json`/`codex-bot-config.json`의 `channel_id`가 동일 — 의도된 설계, 둘 다 모든 메시지를 봐야 각자 자기 명령만 골라 응답할 수 있다). 그래서 라우팅 배제 로직이 정합적이어야 한다 — 한쪽이 처리할 메시지를 다른 쪽이 못 걸러내면 이중 응답이 난다(2026-07-30에 실제로 이 클래스 버그가 발견·수정됨, 아래 "2026-07-30 통합 감사" 절 참고).
- `bin/discord-notify.sh <message>` — 봇 프로세스와 무관하게 Discord REST API로 메시지 한 번 보내는 헬퍼. 실패해도 항상 exit 0 — 알림 실패가 호출한 스크립트(주간보고서 등)를 절대 죽이면 안 됨. Phase 2부터 성공 시 게시된 메시지의 Discord id를 stdout으로 반환(실패 시 빈 문자열) — 호출한 스크립트가 그 id로 pending-job을 기록해 나중에 답장을 매칭할 수 있게 함.
- 설정: `~/.claude/discord-bot/config.json` (`{"token":..., "channel_id":..., "free_chat_user_id":...}`) — 이 레포 밖, `chmod 600`. 토큰은 Discord 개발자 포털(https://discord.com/developers/applications)에서 발급, "Message Content Intent"를 반드시 켜야 봇이 메시지 내용을 읽음. `free_chat_user_id`는 Phase 1부터 미리 넣어둔 값이었고, `!코덱스`(2026-07-28)에서 처음 참조하기 시작했으며 Phase 3(2026-07-29, 자유 채팅)에서도 그대로 재사용한다 — "Claude/코덱스에게 임의 지시를 내릴 수 있는 사람"이라는 같은 권한 레벨을 의미.
- `~/.claude/discord-bot/free-chat-session.json` — Phase 3 세션 상태(레포 밖, git 추적 안 함). `{"session_id": <uuid>, "last_used_at": ISO시각}` 하나만 담는다 — 채널당 자유 채팅 사용자가 한 명뿐이라 여러 대화를 구분할 필요가 없음. `!새대화`로 삭제하면 다음 메시지가 새 세션을 시작한다.
- `~/.claude/discord-bot/free-chat-fallback-context.json` — Claude 한도 때 Antigravity/Codex가 답한 마지막 요청/응답을 bounded하게 저장한다. 다음 Claude 턴에 “신뢰되지 않은 참고자료”로 한 번 주입하고 Claude가 정상 응답하면 삭제한다. `!새대화`에서도 삭제한다.
- `~/.claude/discord-bot/pending/<message_id>.json` — Phase 2 pending-job 저장소(레포 밖, git 추적 안 함). 에스컬레이션을 쏜 스크립트가 `discord-notify.sh`가 반환한 message id로 기록: `{"type":"weekly-report-retry"|"work-log-retry"|"verify-task-v2-retry"|"verify-task-v2-decision-retry","created_at":ISO시각,"params":{...}}` — `weekly-report-retry`는 `params`가 비어있고(자기완결 스크립트라 외부 상태 불필요), `work-log-retry`는 `params`에 `session_id`/`transcript_path`를, `verify-task-v2-retry`는 `params`에 `task`(원본 전체, 자르지 않음)/`cwd`/`persona`/`maxRounds`/`historyFile`/`harnessFile`/`questions`를, `verify-task-v2-decision-retry`는 `questions`만 빼고 동일한 필드를 담는다(각각 재실행에 필요한 상태를 스크립트/워크플로우 자체가 못 들고 있어서). `discord-bot.py`가 답장을 받으면 `message.reference.message_id`로 이 파일을 찾아 `type`에 따라 디스패치하고 처리 후 즉시 삭제(중복 답장 방지). 48시간 지난 항목은 만료 처리. `type`을 못 알아들으면(향후 미구현 소스 등) 조용히 로그만 남기고 무시 — 스키마가 깨지지 않고 확장 가능.

## 권한 경계 (사용자가 명시적으로 결정한 것, 재검토 없이 그냥 넓히지 말 것)

- **인가된 채널만**: `config.json`의 `channel_id` 하나만 반응, 그 외 채널/DM/다른 서버/봇 자신의 메시지는 전부 무시.
- **채널 신뢰의 실제 범위(2026-07-30 정정)**: 원래는 "그 채널은 초대받은 사람 전원이 완전히 신뢰된다"가 신뢰 경계 전부라고 서술돼 있었다. 그런데 Codex의 독립 코드리뷰(2026-07-30)가 `verify-task-v2-retry`/`decision-retry` 답장 재시도는 결과적으로 풀-툴-액세스 `claude -p` 실행까지 이어지므로, 실질적으로 자유 채팅과 동급 권한을 채널 전체에 열어주고 있었다는 점을 지적했다 — 원래 문서 서술과 실제 위험 수준이 안 맞았던 것. 사용자 확인 후, pending-job 답장(`handle_pending_reply`) 전체를 `free_chat_user_id`(본인)로 제한했다. 무단 답장은 job을 건드리지 않고(삭제·소비 안 함, 진짜 소유자가 나중에 여전히 답장 가능) 거부 메시지만 보낸다. 채널 전체 신뢰가 실제로 적용되는 범위는 이제 `!주간보고서`/`!상태` 같은 순수 온디맨드 조회성 명령뿐이다.
- **자유 채팅(Claude Code에 임의 지시)과 pending-job 답장 재시도는 모두 본인만**: Phase 3(2026-07-29)에서 자유 채팅에 먼저 구현됐고(`free_chat_user_id`와 발신자 id 일치 시에만), 2026-07-30에 pending-job 답장 재시도도 동일 게이트로 통일됐다 — 둘 다 결과적으로 같은 수준의 실행 권한(풀-툴-액세스 relay 또는 그에 준하는 워크플로우 재실행)을 열어준다는 점에서 하나의 권한 레벨로 취급하는 게 맞다는 판단.

## Phase 1 명령어

- `!주간보고서` — `weekly-report.sh`를 즉시 실행(최대 20분 watchdog). 완료/실패 결과를 같은 채널에 보고.
- `!상태` — 오늘자 weekly-report 로그 tail + 최근 work-log 처리 3건 요약.
- `!새대화` (Phase 3, 본인 전용) — 자유 채팅 세션 상태 초기화.
- `!중지` (Phase 3, 본인 전용) — 실행 중인 자유 채팅 응답 강제 종료.
- 명령어도 아니고 pending-job에 대한 답장도 아닌 메시지는, 발신자가 `free_chat_user_id`면
  Phase 3 자유 채팅으로 릴레이되고, 그 외엔 전부 무시.

## Phase 2 v1 — weekly-report.sh 답장 재시도

세 에스컬레이션 소스 중 `weekly-report.sh`만 재시도가 안전하다고 판단해(2026-07-28) 이것부터
구현했다: 재실행 자체가 안전하고, 유일한 중복 위험(Calendar 이벤트 매번 새로 생성)은 4번
단계에 `search_events` 선확인 가드를 추가해 막았다. 나머지 둘은 이 시점엔 일방향으로
남겨뒀다:

- `verify-task-v2.js`의 `needsUserDecision`/`needsClarification`은 재개(resume) 메커니즘
  자체가 없다 — 유일한 복구 경로가 "질문에 답 → 전체 워크플로우를 처음부터 재호출"이다.
  둘 다 아래 "## verify-task-v2 답장 재시도" 절에서 해결했다.
- `work-log-stop-check.sh`는 `.dispatched` 마커 때문에 단순 재실행이 즉시 no-op되고, 스크립트
  자체 주석에 "중복 아카이브/캘린더 이벤트 위험 때문에 재시도를 의도적으로 안 만들었다"고
  적혀 있었다 — 아래 "## Phase 2.5" 절에서 이 판단을 실제로 다시 검토해 해결했다.

동작: 실패 알림에 답장(Discord reply) → `discord-bot.py`가 `message.reference`로
`~/.claude/discord-bot/pending/<id>.json`을 찾아 `type: "weekly-report-retry"`를 확인하고
`handle_weekly_report()`를 재사용해 재실행. pending-job은 처리 즉시 삭제(중복 답장 방지),
48시간 지나면 만료 처리. ack 메시지 전송이 실패해도 재시도 자체는 계속 진행(ack는 사후
알림일 뿐 재시도 여부를 좌우하지 않음). 코드 변경 후 launchd 재기동 필요(위 구성 항목 참조).

**동시실행 뮤텍스(2026-07-28 추가)**: `!주간보고서`·답장 재시도·목요일 정기 실행 세 트리거가
겹칠 수 있어, Calendar 검색-선확인 가드만으로는 두 실행이 동시에 각자 "없음"을 보고 이벤트를
중복 생성할 위험이 남아있었다. `weekly-report.sh` 시작 시 `$STATE_DIR/.lock`(PID+mtime)을
확인해 이미 살아있는 실행이 있으면(30분 이내, PID 생존) 새 실행을 **건너뛴다**(exit 3). 정상
성공은 exit 0, 재시도 소진 실패는 exit 1, 건너뜀은 exit 3으로 구분되고, `discord-bot.py`는
exit 3을 "⏳ 이미 실행 중"으로 별도 표시한다(실패로 오표시하지 않음). 락이 30분 넘게 남아있으면
(비정상 종료로 stale) 자동으로 무시하고 새로 잡는다.

**사용량 사전 게이트(2026-07-28 추가, P4)**: 뮤텍스 락을 잡기 **전에**
`workflows/lib/usage-preflight-gate.sh claude`를 먼저 확인한다 — 락 잡고 나서 건너뛰면 락만
불필요하게 쥐는 셈이라 순서가 중요함. `SKIP:`이면 로그에 남기고 `discord-notify.sh`로 사유
그대로(리셋 시간 포함) 알린 뒤 `weekly-report-retry` pending-job을 기록(사용량 회복 후 답장으로
재시도 가능)하고 exit 4. `discord-bot.py`는 exit 4를 별도 처리하되 자체 메시지는 안
보낸다(스크립트가 이미 `discord-notify.sh`로 알렸으므로 중복 알림 방지) — exit 3(뮤텍스)이
스크립트 자체는 알리지 않고 `discord-bot.py`만 알리는 것과 반대 구조. 게이트 스크립트 자체가
실패하면(존재 안 함 등) `|| echo "PROCEED..."`로 fail-open — 게이트 오류가 주간보고서 자체를
막는 새로운 장애점이 되면 안 됨. 코드 리뷰 + 샌드박스 테스트(스크래치 경로로 알림/pending 파일
리다이렉트, 실제 계정 사용량 데이터로 SKIP 분기 재현)로 검증 — 실제 launchd/Discord 트리거로
라이브 실행까지는 안 함(클로드 5시간창이 마침 0%인 상태에서 만들어져 실제 발동 조건 재현은
쉬웠지만, 그 상태에서 라이브 전체 실행까지 태우는 건 이 게이트가 막으려는 걸 스스로 하는
셈이라 의도적으로 안 함).

**검증 상태(2026-07-28, 코드리뷰로 뒤늦게 발견)**: `handle_weekly_report()` 자체는 `!주간보고서`
수동 명령으로 여러 번 실전 실행됐지만, **이 절이 설명하는 "답장 → pending-job 조회 →
재실행" 디스패치 경로 자체는 실제 Discord 답장으로 라이브 실행된 적이 없다** — 봇 로그에도
`weekly-report-retry` 타입이 처리된 흔적이 없음. 코드 리딩상으론 work-log-stop-check.sh
재시도(합성 세션 3개로 검증됨, 아래 Phase 2.5 절)와 구조가 거의 동일해 위험은 낮다고 보지만,
"검증됨"이라고 문서가 암묵적으로 읽히던 걸 바로잡는다 — 실제 실패 알림에 답장해보는 라이브
테스트가 아직 남아있다.

## Phase 2.5 — work-log-stop-check.sh 답장 재시도

원래 재시도를 안 만든 이유(스크립트 주석)는 "재실행이 아카이브 파일/캘린더 이벤트를 중복
생성할 수 있다"였는데, 실제로 뜯어보면 **두 산출물의 중복 위험이 서로 다르다**:

- **아카이브 파일 복사는 원래부터 멱등적이다** — 프롬프트가 원본 파일을 `.../YYYY-MM-DD/`
  날짜 폴더로 "복사"만 시키는데, 같은 소스 파일명은 같은 목적지 경로로 재실행해도 그냥
  덮어쓰기만 된다. 손댈 필요가 없었다.
- **진짜 위험은 캘린더 이벤트 하나뿐**이었다 — 매번 "새로 생성해라"고만 지시돼 있어 존재
  확인 없이 무조건 새로 만들었다. `weekly-report.sh`의 `search_events` 선확인 가드와
  동일한 발상으로 막았다: 이벤트 description 맨 끝에 검색 가능한 `[세션ID: <id>]` 마커를
  반드시 남기게 하고, 새로 만들기 **전에** 오늘 날짜로 그 마커가 포함된 기존 이벤트가
  있는지 먼저 찾아서 있으면 update, 없으면만 생성하도록 프롬프트를 바꿨다.

나머지 두 가지 실제 구현 문제:

- **`.dispatched` 마커**: 최초 1회 찍히면 이후 같은 세션ID는 스크립트 맨 위에서 즉시
  `exit 0`(정리 로직 없음). 재시도 시 `discord-bot.py`의 `handle_work_log_retry()`가
  `$STATE_DIR/work-log/<session_id>.dispatched`를 먼저 지우고 시작한다.
- **stdin 전용 입력**: 이 스크립트는 Claude Code Stop 훅 전용으로 설계돼 stdin JSON
  (`{"session_id":..., "transcript_path":...}`)만 받는다. `weekly-report.sh`처럼 "그냥
  재실행"이 안 돼서, 최초 실패 시 pending-job의 `params`에 이 두 값을 담아뒀다가 재시도
  때 합성 stdin으로 다시 흘려보낸다.

**완료/실패 알림 자체가 원래 없었다**: 기존엔 타임아웃 실패에만 알림이 갔고, 성공이나
타임아웃 아닌 실패는 로그 파일에만 조용히 남았다(재시도 호출자가 결과를 알 방법이 없었음).
이제 세 갈래 다 알림: 성공 시 "✅ 완료"(단, 실제로 "LOGGED:"를 출력한 경우만 — 세션이
`SKIP` 판정났을 때까지 매번 알리면 스팸이 되므로 제외), 실패 시 pending-job과 함께
"⚠️ 실패, 답장하면 재시도" 알림. 스크립트가 `( ... ) & disown`으로 실제 작업을
백그라운드에 넘기고 즉시 반환하는 구조라, `handle_work_log_retry()`는 재시도 디스패치
자체가 정상 시작됐는지만 확인하고, 진짜 성공/실패는 이 새 알림 경로로 별도로 온다.

**사용량 사전 게이트(2026-07-28, P4)**: `.dispatched` 마커를 세운 **직후**(찍기 전이 아니라)
`usage-preflight-gate.sh claude`를 확인한다 — 이 훅은 세션 하나당 한 번만 도는 구조라 마커는
그대로 세워 같은 세션에 대해 Stop이 다시 떠도(`/resume` 등) 재시도하지 않게 하고, 대신 복구는
기존 타임아웃/실패 경로와 동일하게 `write_pending_job()`으로 위임한다 — 답장이 오면
`handle_work_log_retry()`가 저장된 `session_id`/`transcript_path`로 직접 재실행하니 이 마커와
무관하게 동작한다. 게이트 자체가 실패하면 fail-open으로 그냥 진행.

## verify-task-v2 답장 재시도 — needs_clarification + needsUserDecision (2026-07-28)

`verify-task-v2.js`는 앞의 둘(weekly-report.sh, work-log-stop-check.sh)과 두 가지가
근본적으로 다르다: (1) bash 스크립트가 아니라 Claude Code의 `Workflow` 툴로만 실행 가능한
JS 워크플로우라 discord-bot.py가 그냥 서브프로세스로 못 띄운다. (2) 재개(resume) 메커니즘
자체가 없어서 "부분 이어하기"가 아니라 항상 **전체를 처음부터 재실행**해야 한다.

`finalVerdict`가 멈추는 경우는 두 가지고, 이제 둘 다 구현돼 있다:

### needs_clarification (정보 부족 역질문)

- `verify-task-v2.js`의 `notifyDiscordEscalation()`이 `discord-notify.sh`의 반환 메시지
  id를 캡처해서(Workflow 스크립트는 JS 샌드박스라 파일시스템에 직접 못 씀 —
  `appendHistory`/`appendHarnessRules`와 같은 방식으로 `agent()`를 통해 Bash/Write로 대신
  시킴) `type: "verify-task-v2-retry"` pending-job을 기록한다. `params`에 원본 `task`(자르지
  않은 전체), `cwd`, `persona`, `maxRounds`, `historyFile`, `harnessFile`, `questions`를
  담는다 — 재실행에 필요한 상태 전부.
- 답장이 오면 `handle_verify_task_v2_retry()`가 답장 텍스트를 `task` 끝에
  `"\n\n[사용자 답변]\n" + 답장`으로 붙여서 새 task를 만들고, 같은 maxRounds로 처음부터
  재실행한다.
- 재시도한 실행이 또 `needs_clarification`으로 끝나면, 그 재실행 안의
  `notifyDiscordEscalation()`이 다시 독립적으로 발동해서 새 pending-job을 만든다 —
  별도 코드 없이 "답변→또 불명확→다시 답변" 체인이 자연스럽게 반복 가능하다.

### needsUserDecision (최대 라운드 소진) — 키워드 감지 방식 (2026-07-28)

최초 구현 시(같은 날 이전 커밋) 이 경로는 의도적으로 범위에서 뺐다 — "수용/재시도/수동개입"
3지선다를 Discord의 자유텍스트 답장으로 의미 해석하는 게 애매해서였다. 사용자에게 다시
확인한 결과(2026-07-28), 전체 자연어 이해 대신 **답장에 재시도 의도 키워드(`재시도`/`retry`/
`다시`)가 있는지만 감지**하는 방식으로 좁혀서 구현하기로 함:

- `type: "verify-task-v2-decision-retry"` pending-job을 needs_clarification과 같은 스키마로
  기록(단 `questions` 필드 없음 — 답할 질문이 없으므로).
- `handle_verify_task_v2_decision_retry()`가 답장 텍스트에서 `_has_retry_intent()`로 키워드
  존재만 확인한다. 키워드가 있으면 `maxRounds`를 +2 늘려서(질문에 답하는 게 아니므로 task는
  원본 그대로, 붙이는 텍스트 없음) 처음부터 재실행. 없으면("수용", "수동으로 할게", 그 외
  아무 말) 자동 조치 없이 "확인했습니다" 답장만 하고 종료 — "수용"과 "수동개입"을 굳이
  구분하지 않는다, 둘 다 "더 이상 자동으로 건드리지 마라"는 뜻은 같기 때문.
- 재시도가 또 `needsUserDecision`으로 끝나면 마찬가지로 새 pending-job이 독립적으로 다시
  생겨 답장 체인이 반복 가능.

### 공통 구현 메모

- **discord-bot.py가 Claude Code의 `Workflow` 툴을 직접 호출한 최초 사례** — 이전엔 bash
  스크립트 직접 실행(`weekly-report.sh`, `work-log-stop-check.sh`)이나 `codex exec` 직접
  호출(`!코덱스`)뿐이었다. headless `claude -p`에 자연어로
  `Workflow({scriptPath: ".../verify-task-v2.js", args: {...}})`를 호출하라고 지시하는
  방식 — 실현 가능성은 사전에 트리비얼한 probe 워크플로우로 실측 확인 후 이 설계를 그대로
  밀어붙였다.
- work-log-stop-check.sh의 재시도(`handle_work_log_retry`)와 달리 **끝까지 기다린다**
  (`stdout=PIPE`+`communicate()`) — verify-task-v2는 `& disown`으로 백그라운드에
  넘기는 구조가 아니라 `claude -p` 프로세스 자체가 끝까지 동기적으로 도는 구조라,
  work-log-stop-check.sh에서 겪었던 "disown된 자식이 파이프를 붙잡고 있어서 블로킹"
  문제가 여기엔 적용되지 않는다(그 버그와 이유는 `handle_work_log_retry`의 docstring 참고).
- **`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` 필수** — 이게 없으면 `claude -p`가 Workflow의
  백그라운드 `agent()` 호출들을 ~600초까지만 기다리다 포기하고 "백그라운드에서 계속 실행
  중이니 나중에 알려줄게"라며 exit 0으로 끝나버리는데, 일회성 `-p` 프로세스는 그 "나중에"를
  전달할 방법이 없어 재시도가 사실상 유실된 채로 거짓 성공 취급되는 버그가 실측으로 확인됨.
  이 환경변수로 그 give-up 동작 자체를 꺼서 `communicate()`가 워크플로우가 실제로 끝날 때까지
  진짜로 블로킹하게 만든다.
- 타임아웃 30분(`!코덱스`와 동일 — 전체 트랙이 코덱스/안티그래비티를 여러 번 오가므로).
- **사용량 사전 게이트(2026-07-28, P4)**: 두 핸들러 다 실제 `claude -p`를 스폰하기 직전에
  `usage_gate_check("claude")`(discord-bot.py 공용 헬퍼, `!코덱스`와 공유)로 확인한다. 이
  둘은 이미 pending-job 답장 체인 안에 있는데, `handle_pending_reply()`가 디스패치 **전**에
  원본 pending-job을 지우는 구조라("delete first so a second reply can't double-trigger")
  게이트에서 그냥 안내만 하고 끝내면 사용자가 "다시 답장"해도 아무 일도 안 일어난다 — 그래서
  `send_and_requeue()`로 방금 보낸 안내 메시지 자체에 새 pending-job을 다시 걸어(같은
  type/params), 그 메시지에 답장하면 체인이 이어지게 한다. needs_clarification 쪽은 답변
  텍스트 자체를 저장 안 하므로("이 메시지에 답변을 다시 담아 답장해주세요") 재답장 시 새로
  입력해야 함을 안내에 명시.

**실측 검증**: needs_clarification은 scratch repo에 의도적으로 정보가 부족한 작업("사용자
인증 기능을 추가해줘", 아무 컨텍스트 없는 빈 저장소)을 줘서 full 트랙에서 실제로 발생하는 걸
확인함(코덱스가 인증 방식/기술 스택/범위 3개 필수 질문을 정확히 생성). `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0`
수정은 실전 재시도 테스트가 26분(1573초) 동안 정상적으로 끝까지 대기하다가 진짜 실패
사유(계정 세션 한도)를 정확히 보고하는 것으로 확인(예전엔 600초에 끊겨 거짓 완료를 보고).
`needsUserDecision`의 키워드 감지(`_has_retry_intent`)는 로컬 유닛 테스트로 검증(수용/수동
표현은 False, "재시도"/"retry"/"다시" 포함 표현은 True) — 이 경로 자체가 실제 코덱스/안티를
호출하는 전체 재시도까지 라이브로 도는 건 아직 실측 안 됨(다음에 실제로 needsUserDecision이
발생하면 확인할 것). **부정문 오탐 수정(2026-07-28, 코드리뷰로 발견, 라이브에서 걸리기 전에
잡음)**: 최초 구현은 순수 부분문자열 매칭이라 "재시도 필요 없어"/"다시 안 해도 돼"/"no need
to retry"처럼 키워드는 포함하지만 의미는 반대인 답장도 재시도로 오판했다. `RETRY_NEGATION_MARKERS`
목록(다의미 단일 음절 "안"/"않" 대신 "필요없"/"안 해도"/"하지마" 같은 구체적 표현만 사용 —
"안녕" 같은 무관한 단어에 걸리지 않게)을 추가해, 키워드+부정 마커가 같이 나오면 재시도하지
않고 무조치로 fail-closed 처리하도록 수정. 16개 케이스(참/거짓 양쪽 + 부정문 8종)로 유닛
테스트 검증 완료 — 라이브 재시도 경로 자체의 미검증 상태는 위와 동일하게 남아있음.

**사용량 게이트 실측 검증(2026-07-28)**: 두 핸들러 다 가짜 `discord.Message`로 실제 함수를
직접 호출해 검증 — 이 시점 실제 계정이 클로드 5시간창 0%라 진짜 SKIP 분기를 그대로 탔다.
`send_and_requeue()`가 만든 새 pending-job 파일의 `type`/`params`가 정확한 것까지 확인.
`!코덱스`의 코덱스 액터 체크는 반대로 실제 코덱스 사용량이 넉넉해서(7일창 90%) 진짜 PROCEED로
흘러 실제 코덱스가 mac-agent 저장소에 대고 한 번 돌았다(의도한 테스트는 아니었음 — 코덱스
액터도 SKIP 될 거라 잘못 가정했었다; 프롬프트가 무의미해서 실제 파일 변경은 없었고
`git status`로 무해함 확인함). SKIP 분기는 별도로 `usage_gate_check`를 몽키패치해 안전하게
재검증(코덱스 서브프로세스가 실제로 안 뜨는지 assert까지 포함).

## `!코덱스` 계열 명령 — 코덱스 직접 디스패치·대화 (본인 전용, 2026-07-28, 2026-07-29에 `codex-bot.py`로 이전)

**아래 내용은 전부 `bin/codex-bot.py`(별도 프로세스)에서 실행된다** — 원래 `discord-bot.py`
안에서 구현됐다가 2026-07-29 "Split Codex-related Discord commands into a dedicated bot
process" 커밋으로 옮겨졌다. 메커니즘 서술(before/after 델타 검증, 락, 타임아웃 등)은
이전 후에도 그대로 유효해서 아래 원문을 고치지 않고 남겨뒀다 — 파일 위치만 바뀐 것으로 이해할 것.

```
!코덱스 <저장소별칭> <작업 지시>
```
예: `!코덱스 mac-agent discord-bot.py에 로그레벨 옵션 추가해줘`

지금까지 디스코드가 실행시키는 건 전부 `claude -p` 경로(주간보고서 등)뿐이었는데, 코덱스로
가는 길이 없었다. 이 명령어로 디스코드에서 코덱스에게 직접 코딩 작업을 맡길 수 있다.

- **재사용**: `workflows/lib/codex-execute-dispatch.sh <cwd> <prompt-file>`를 그대로
  사용(verify-task-v2용으로 이미 있던 write-capable 코덱스 실행기) —
  `codex exec -s workspace-write -C <cwd> "$(cat <prompt-file>)"`를 실행하고
  `{"ok": bool, "message": string}` JSON을 반환한다(2026-07-29: cwd가 git 저장소인지
  직접 검증하는 가드가 추가되면서 `--skip-git-repo-check`는 제거됨 — 상세는
  `docs/verify-task-v2-design.md` 참고).
- **저장소 별칭 allowlist**(`CODEX_REPO_ALIASES`, `discord-bot.py`): 임의 절대경로를
  그대로 받으면 `workspace-write`가 엉뚱한 곳을 건드릴 위험이 있어, 미리 승인된 별칭만
  허용. 현재: `mac-agent`, `hwpx-skill`, `pptx-skill`. 새 저장소 필요하면 이 딕셔너리에
  한 줄 추가(사용자 확인 후).
- **권한 게이트**: `config.json`의 `free_chat_user_id`(Phase 1부터 예비해뒀던 필드, 이제
  실제로 사용 시작)와 발신자 id가 일치할 때만 허용. 채널 신뢰만으론 부족한 임의 코드실행급
  명령이라 별도 게이트 — 불일치 시 조용히 무시하지 않고 거부 메시지를 보낸다.
- **자기 보고 불신 + before/after 델타 검증(2026-07-28 실사용 중 수정)**: 처음엔 실행 후
  `git diff --stat` 한 번만 돌렸는데, 실사용 첫 테스트에서 바로 문제가 드러났다 — README에
  한 줄만 추가해달라고 시켰는데, 그 시점에 다른 터미널이 같은 저장소에서 작업 중이던
  무관한 파일 3개까지 "변경됨"으로 같이 보고됐다. 코덱스 실행 **전**과 **후** 각각
  `_dirty_snapshot()`(추적 파일은 `git diff` 텍스트, 미추적 신규 파일은 상태만)을 찍어서,
  두 스냅샷 사이에 실제로 달라진 파일만 골라 보여주도록 고쳤다. 이미 있던 미커밋 변경
  사례는 이걸로 해결되지만, **이 실행이 도는 동안** 다른 프로세스가 같은 파일을 동시에
  건드리는 경우까지는 diff 비교만으로 완전히 못 잡는다 — 같은 작업 트리를 동시에 여러
  터미널/에이전트가 건드리는 구조적 한계이고, 완전한 격리(별도 git worktree 등)는 아직
  안 함.
  - **미추적 파일의 삭제·내용변경이 안 잡히던 결함(2026-07-29, 코드리뷰로 발견, 라이브 전
    수정)**: 미추적 파일은 스냅샷에 `"UNTRACKED"`라는 고정 문자열만 저장돼 있었다 — 그
    말은 **경로가 미추적 상태라는 사실만** 기록하고 실제 내용은 전혀 안 담았다는 뜻. 두
    가지 실제 케이스가 조용히 안 잡혔다(둘 다 로컬 재현으로 확인): (1) 실행 전부터 있던
    미추적 파일을 코덱스가 `git add` 없이 내용만 바꾸면 before/after가 똑같이
    `"UNTRACKED"`라 변경이 아예 감지 안 됨. (2) 실행 전부터 있던 미추적 파일을 코덱스가
    삭제하면 `git diff`/`git status --porcelain` 어디에도 흔적이 안 남아 `after`
    스냅샷에서 키 자체가 사라지는데, 비교 로직이 `after`의 키만 순회해서 이것도 놓침 —
    두 경우 다 "실제 파일 변경 없음"으로 잘못 보고될 수 있었다. `_hash_file_content()`로
    미추적 파일도 실제 내용의 sha256 해시(`"UNTRACKED:<hash>"`)를 저장하고, 비교도
    `after`만이 아니라 `before`∪`after` 전체 키를 대상으로 하도록 고침 — 신규/삭제/내용변경
    세 경우를 각각 "신규 파일"/"삭제됨(기존 미추적 파일)"/"내용 변경(미추적 파일)"로 구분해
    보고한다. 실제 스텁 코덱스로 4가지(추적 파일 수정, 미추적 파일 삭제·내용변경·신규생성)를
    동시에 일으키는 종단 테스트로 전부 정확히 보고되는 것까지 확인.
- **커밋/푸시 안 함**: diff까지만 보여주고 끝 — 커밋·푸시는 별도의 명시적 요청이 있을 때만
  (되돌리기 어려운 작업이라 자동화하지 않음).
- **재시도 없음(의도적)**: `weekly-report.sh`와 달리 자동 재시도가 없다 — 코딩 작업은
  실패 후 재실행하면 부분 변경이 누적될 수 있어, 실패하면 결과(+diff)만 보여주고 사용자
  판단에 맡긴다.
- **저장소별 동시 실행 락(2026-07-29, 라이브 전에 코드리뷰로 발견)**: `_dirty_snapshot()`의
  before/after 델타 자체가 "실행 도중 다른 프로세스가 같은 파일을 건드리는 경우까지는 완전히
  못 잡는다"고 이미 인정하고 있었는데, 그 "다른 프로세스"가 이 봇 자신일 수 있다는 걸 처음엔
  안 막았음 — 같은 별칭으로 `!코덱스`를 연달아 보내면 같은 저장소에 코덱스가
  `workspace-write`로 두 번 동시에 돌 수 있었다. `CODEX_DISPATCH_LOCKS`(**정규화된 실제
  경로**별 `asyncio.Lock` — 최초엔 별칭 문자열로 키를 잡았다가, `CODEX_REPO_ALIASES`에
  서로 다른 별칭 두 개가 같은 실제 경로를 가리키지 않는다는 보장이 코드 어디에도 없어서
  나중에 `cwd.resolve()`로 교체함, 재검토로 발견)로 고침 — 이미 잠긴 경로면 대기 없이 즉시
  거부, 실제로 다른 경로끼리는 서로 상태를 안 공유하므로 그대로 병렬 허용(자유 채팅의
  `FREE_CHAT_LOCK`과 같은 클래스 버그,
  같은 "대기 대신 거부" 해법).
- **타임아웃**: 30분(`CODEX_DISPATCH_TIMEOUT_SECONDS`) — `codex exec`엔 자체 타임아웃
  플래그가 없어(`--help`에 없음 확인됨) 호출자 쪽(`asyncio.wait_for`)에서 건다.
  **프로세스 그룹 종료(2026-07-29, 코드리뷰로 발견)**: `codex-execute-dispatch.sh`는
  `RAW_OUTPUT="$(codex exec ...)"`(명령어 치환)으로 코덱스를 부르므로 실제 코덱스는 이
  bash 스크립트의 자식이다 — 타임아웃 때 `proc.kill()`로 그 bash만 죽이면 실제
  `workspace-write` 코덱스 프로세스는 백그라운드에서 계속 살아있는데도 사용자에겐 "강제
  종료했다"고 알리는 셈이었다(로컬 재현으로 확인: 명령어 치환으로 오래 걸리는 자식을 배경에
  둔 bash 스크립트가 plain `proc.kill()`엔 자식을 살려둔 채 살아남음). 자유 채팅과 동일하게
  `start_new_session=True`+`_kill_process_group()`으로 교체 — 실제 코덱스 프로세스까지
  같이 정리되는 것을 동일한 구조의 가짜 디스패치 스크립트로 재현·확인.
- **2단계 truncation 방향 불일치(2026-07-29, 코드리뷰로 발견 — 지난 수정을 스스로 무효화하고
  있었음)**: `codex-execute-dispatch.sh`는 실패 시 끝 2000자를 반환하도록 이미 고쳐뒀는데,
  `discord-bot.py`가 그걸 받아서 `codex_message = str(result.get("message", ""))[:1000]`로
  **또 앞부분만** 잘라서, bash 스크립트가 보존해둔 진짜 에러(끝부분)를 다시 잘라버리고
  있었다. `[-1000:]`(끝부분)로 교체, JSON 파싱 실패 폴백 분기(`raw[:1000]`)도 같은 이유로
  동일하게 수정. 가짜 코덱스 스텁(배너+150줄 작업로그+끝에 진짜 에러)으로 실제
  `handle_codex_dispatch` 전체 파이프라인을 통과시켜, 최종 Discord 메시지에 진짜 에러 문구가
  살아남는 것까지 확인.
- **사용량 사전 게이트(2026-07-28, P4)**: 저장소 별칭 확인 직후, 코덱스를 실제로 부르기 전에
  `usage_gate_check("codex")`(공용 헬퍼, `verify-task-v2` 답장 재시도 두 핸들러와 공유)로
  확인한다. `!코덱스`는 pending-job 기반 재시도 체인이 아예 없는 일회성 수동 명령이라, SKIP이면
  그냥 안내만 하고 끝 — 사용량 회복 후 사용자가 직접 `!코덱스`를 다시 보내면 된다.

**사전 스모크테스트(2026-07-28)**: 이전까진 유일한 검증 기록이 "실사용 중 버그 발견"이라
사전 테스트가 아니라 프로덕션에서 걸린 것이었다. 뒤늦게 채워넣음 — 실제 프로젝트를 건드리는
위험 없이 검증하려고, 처분 가능한 scratch git 저장소를 만들어 `CODEX_REPO_ALIASES`에
임시 별칭으로 넣고(코드 자체는 안 건드림, 테스트 스크립트에서만 몽키패치) 가짜
`discord.Message`로 `handle_codex_dispatch()`를 직접 호출해 3가지 확인:
1. **정상 경로**: README에 한 줄 추가 지시 → 코덱스가 정확히 그 한 줄만 추가, before/after
   델타가 실제 `git diff`와 정확히 일치하게 보고, 커밋은 안 됨(작업 트리에 미커밋 변경으로만
   남음) — 설계대로 동작.
2. **권한 게이트**: `FREE_CHAT_USER_ID`와 다른 사용자 id로 호출 → 코덱스 실행 자체를 시도하지
   않고 즉시 거부 메시지.
3. **별칭 allowlist**: 등록 안 된 저장소 별칭으로 호출 → 마찬가지로 즉시 거부, 사용 가능한
   별칭 목록 표시.
셋 다 통과. scratch 저장소는 테스트 후 삭제.

### `!코덱스대화` / `!코덱스대화초기화` — 연속 대화 (본인 전용, `codex-bot.py` 분리와 함께 신설, 이전까지 이 문서에 전혀 기록 안 돼 있었음)

```
!코덱스대화 <저장소별칭> <메시지>
!코덱스대화초기화 <저장소별칭>
```

`!코덱스`(단발 디스패치)와 별개로, 하나의 저장소 별칭에 대해 여러 턴에 걸친 대화를 이어갈 수
있는 명령. 세션 상태는 `~/.claude/discord-bot/codex-chat-session-<alias>.json`류 파일에
`thread_id`로 저장되고(`_codex_chat_session_path`/`_load_codex_chat_thread_id`/
`_save_codex_chat_thread_id`), `CODEX_DISPATCH_LOCKS[resolved_cwd]`(위 `!코덱스`의 동시실행
락과 동일한 락, alias가 아니라 정규화된 실제 경로로 키를 잡음)로 리셋 중 저장/저장 중 리셋
경쟁을 막는다. `on_message` 라우팅은 `"!코덱스대화초기화"`를 `"!코덱스대화"`보다 먼저
체크해야 한다(둘 다 `"!코덱스"` 접두어를 공유해서, 순서가 바뀌면 초기화 명령이 항상 일반
대화로 잘못 라우팅됨 — `elif` 체인이라 순서가 로직 그 자체).

이름 대신 자연어로 부르는 방식(`CODEX_CHAT_WAKE_WORDS = ("코덱스", "콕스")` — "코덱스야
...", "콕스 ...")도 같은 대화 상태를 공유하며 지원된다(`handle_codex_chat_wake`) — 저장소
별칭 지정 없이 대화 맥락에서 이어가는 짧은 형태.

**타임아웃**: `CODEX_CHAT_TIMEOUT_SECONDS`(30분), `!코덱스`와 동일한 관대한 여유.

### `!코덱스중지` — 실행 중단 (본인 전용, 2026-07-30, 기능 패리티 갭 해소)

```
!코덱스중지 <저장소별칭>
```

`discord-bot.py`의 `!중지`(자유채팅 중단)에 대응하는 명령이 이쪽엔 없어서, 오래 걸리는
`!코덱스`/`!코덱스대화`를 중간에 멈추려면 `CODEX_DISPATCH_TIMEOUT_SECONDS`/
`CODEX_CHAT_TIMEOUT_SECONDS`(각 30분) 타임아웃을 그냥 기다려야 했다. `CODEX_CURRENT_PROCS`
(정규화된 cwd별 dict — `!중지`의 `FREE_CHAT_CURRENT_PROC`과 같은 목적이지만, 여러 별칭이
독립적으로 동시에 돌 수 있어 단일 전역이 아니라 dict)에 발행된 실행 중인 프로세스를
`_kill_process_group_graceful`로 정리한다. `CODEX_DISPATCH_LOCKS`도 함께 확인해서, 락은
잡혔지만 아직 실제 프로세스가 안 뜬 짧은 창(사용량 게이트 대기 등)에 "실행 중인 게 없다"고
잘못 알리지 않는다(discord-bot.py의 `!중지`가 `FREE_CHAT_LOCK`도 같이 확인하는 것과 같은
이유).

## Phase 3 — 자유 채팅 (본인 전용, 2026-07-29)

`config.json`의 `free_chat_user_id`에 해당하는 사용자가 이 채널에 보내는, 인식된 명령어도
아니고 pending-job 답장도 아닌 모든 메시지가 자유 채팅으로 릴레이된다. 원 계기는 사용자가 본
유튜브의 Claude Cowork 데모(자연스러운 채팅으로 어디서나 Claude를 부림)를 구독형 Claude
Code(CLI)로도 재현할 수 있는지 물은 것 — `!코덱스`가 "코딩 작업 하나를 코덱스에게"였다면
Phase 3은 그보다 훨씬 넓은 "뭐든 클로드에게, 대화하듯"을 목표로 한다. 설계 갈림길 3개를
사용자에게 직접 확인받고 결정함(2026-07-29):

1. **트리거 — 접두어 없음**: `!채팅 <메시지>`처럼 명시적 접두어를 요구하는 대신, 그 사용자가
   보내는 모든 비-명령어 메시지를 그대로 릴레이. Cowork 같은 자연스러운 대화 느낌을 위해
   선택 — 대신 그 사용자가 채널에서 다른 사람과 잡담해도 지시로 해석될 수 있다는 트레이드오프를
   사용자가 인지하고 감수함.
2. **도구 권한 — 전체 허용**: `!코덱스`처럼 저장소 allowlist로 범위를 좁히는 대신, 인터랙티브
   세션과 동일하게 Edit/Write/Bash 등 전체 도구를 그대로 씀. 안전 경계는 저장소 제한이 아니라
   `free_chat_user_id` 하나뿐 — 사용자가 명시적으로 이 쪽(범위 제한보다 신뢰 기반)을 선택함.
3. **대화 연속성 — `--resume` 유지**: 각 Discord 메시지가 독립된 새 세션이 아니라, 하나의
   이어지는 대화로 유지됨(아래 구현 참고).

**구현**:
- `handle_free_chat()`: 세션 상태가 없으면(첫 메시지, 또는 `!새대화` 직후) Python
  `uuid.uuid4()`로 새 id를 만들어 `claude -p "<메시지>" --session-id <uuid>`로 시작. 세션
  상태가 있으면 `claude -p "<메시지>" --resume <uuid>`로 이어감. 세션 id는 Claude의 출력을
  파싱해서 얻는 게 아니라 우리가 미리 만들어 넘기는 방식 — `--session-id`가 "이 UUID로 새
  세션을 시작하라"는 뜻이라 가능한 접근. **실행이 실패(exit≠0)하면 세션 id를 저장하지 않는다**
  — 시작도 못 한 세션을 이후 메시지들이 계속 resume 시도하게 되는 걸 방지.
- `!새대화`: `free-chat-session.json`을 삭제해서 다음 메시지가 새 세션으로 시작하게 함.
  대화 주제를 바꾸고 싶을 때 필요 — 없으면 무한정 이전 맥락을 끌고 감. `handle_codex_dispatch`와
  같은 방식으로 핸들러 내부에서 자체적으로 `free_chat_user_id` 검사(디스패치 지점의 조건이
  아니라) — 코드리뷰로 발견: 원래 `on_message`의 `!새대화` 분기엔 발신자 검사가 아예 없어서
  아무 채널 멤버나 남의 대화 상태를 초기화할 수 있는 구멍이었음, 핸들러 안으로 옮겨 막음.
- `!중지` (2026-07-29): `FREE_CHAT_CURRENT_PROC`(락을 쥐고 있는 동안만 채워지는, 현재 실행
  중인 서브프로세스 핸들)를 죽인다. 코루틴/태스크를 취소하는 게 아니라 OS 프로세스만 죽이는
  방식 — 그러면 `handle_free_chat()`의 `await proc.communicate()`가 자연스럽게 반환되고,
  이미 있던 `proc.returncode != 0` 분기가 알아서 실패로 보고한다.
  - **상태 3분기(2026-07-29, 재검토로 발견)**: `FREE_CHAT_LOCK`은 사용량 게이트 확인 전에
    잡히는데 `FREE_CHAT_CURRENT_PROC`는 실제 서브프로세스가 뜬 뒤에야 채워져서, 그 사이(짧지만
    실측으로 확인된 진짜 구간)에 `!중지`를 누르면 "실행 중인 게 없다"고 잘못 답하는 문제가
    있었다. 이제 락/proc 상태를 같이 봐서 3가지로 구분: proc 있음 → 죽임 / 락만 있음 →
    "준비 중, 잠시 후" / 둘 다 없음 → "실행 중인 게 없음".
  - **프로세스 그룹 종료(2026-07-29, 재검토로 발견)**: `proc.kill()`은 직계 자식 하나만
    죽이는데, 전체 도구 허용인 자유 채팅은 Bash 툴로 손자 프로세스(오래 걸리는 명령 등)를 얼마든지
    띄울 수 있어서, 그 손자가 고아로 계속 살아있을 수 있었다 — 로컬 재현으로 확인(플레인
    `proc.kill()`은 백그라운드 손자를 살려둠, `start_new_session=True`+`os.killpg`는 깨끗이
    정리됨). 자유 채팅 서브프로세스만 `start_new_session=True`로 띄우고
    `_kill_process_group()`(`os.killpg`, 실패 시 `proc.kill()`로 폴백)로 타임아웃·`!중지`
    양쪽 다 교체 — 이 파일의 다른 `kill()` 지점(주간보고서/코덱스/verify-task-v2 타임아웃)은
    이번 범위에서 안 건드림, 명시적 "중단" 명령이 있는 건 자유 채팅뿐이라 우선순위를 거기 둠.
- **동시 실행 락(2026-07-29, 라이브 전에 코드리뷰로 발견)**: `handle_free_chat()`은 시작할 때
  세션 id를 읽고 최대 30분짜리 서브프로세스가 끝난 뒤에야 저장하는데, discord.py가 메시지마다
  별도 태스크로 동시 실행하는 구조라 첫 응답을 기다리는 중에 자연스러운 후속 메시지를 보내면
  두 번째 호출이 저장 전의 낡은 세션 id를 읽고 완전히 별개의 새 세션을 또 열어버린다 —
  나중에 끝난 쪽이 세션 파일을 덮어써서 진 쪽 응답이 조용히 유실되고 대화가 사용자도 모르게
  갈라진다. `weekly-report.sh`가 겪었던 것과 같은 클래스의 문제인데 그 교훈이 여기엔 처음엔
  적용 안 돼 있었음. `FREE_CHAT_LOCK`(`asyncio.Lock`)으로 고침 — 이미 잠겨 있으면 대기열에
  넣지 않고 즉시 "처리 중" 안내 후 거부(30분짜리 요청이 쌓이는 게 대기보다 나쁘다고 판단).
  `!새대화`도 같은 락을 확인해서, 실행 중 리셋하면 그 실행이 나중에 세션을 다시 저장해버려
  리셋이 조용히 무효화되던 문제를 같이 막음.
- **`--permission-mode` 오버라이드 없음**: 이 파일의 다른 모든 헤드리스 `claude -p` 호출과
  동일하게 아무 것도 안 정함 — 비대화형 기본 동작 + 이 맥의 `~/.claude/settings.json` 권한
  설정에 그대로 맡김. `--dangerously-skip-permissions`류는 검토했지만 이 리뷰의 사용량 예산
  안에서 실제로 검증할 방법이 없어 도입 안 함(새로운 미검증 위험을 추가하는 셈이라서).
- 고정 cwd는 `$HOME`(`!코덱스`처럼 저장소 allowlist가 없으므로 "시작 위치"일 뿐 — 도구 자체는
  절대경로면 어디든 접근 가능, 안전 경계는 위 1번 트리거·`free_chat_user_id` 게이트임).
- 계정 사용량 사전 게이트(`usage_gate_check("claude")`, `!코덱스`/verify-task-v2 재시도와
  공유)를 여기도 그대로 적용. 타임아웃 30분(`!코덱스`와 동일).

**Claude 한도 폴백 톱니(2026-07-30)**: 자유채팅에서 Claude 사전 게이트가 막히거나
Claude의 짧은 재시도까지 quota 오류로 끝나면, `discord-bot.py`는 같은 사용자 메시지를
Antigravity에 먼저 전달한다. Antigravity는 `coach`에서 신뢰할 수 있는 잔여량이 보이지
않으므로 수치 게이트를 만들지 않고, 빈 응답/비정상 종료/짧은 quota 오류를 결과로 판정한다.
Antigravity가 실패할 때만 Codex 7일창 사전 게이트를 확인한 뒤 Codex를 실행한다. 두 대체
provider가 모두 실패하면 provider별 진단을 포함한 Discord 1900자 이내 실패 봉투를 보낸다.
모든 대체 실행은 `FREE_CHAT_LOCK` 안에서 순차적으로 진행하며, 실제 timeout은 process
group 전체를 종료한다. 이 경로는 응답 폴백이고, 코딩 작업의 계획/비평/실행 검증 역할은
기존 `verify-task-v2`를 그대로 따른다. 상세 기준선은
[`provider-gear-mesh-audit.md`](provider-gear-mesh-audit.md)에 기록했다.

**실측 검증(2026-07-29)**: 실제 계정이 이 시점 클로드 5시간창 0%라, 사용량 게이트가 진짜
SKIP을 태우는 것까지 가짜 `discord.Message`로 확인(claude -p 스폰 자체가 안 일어나는 것을
spawn 가드로 assert). PROCEED 경로는 `claude -p` 서브프로세스 자체를 모킹해서(실제 헤드리스
세션은 안 띄움 — 자유 채팅이 실제 세션을 재귀적으로 새로 띄우는 구조라 라이브 테스트가
자기 자신을 또 부르는 형태가 돼 특히 조심스러움) 세션 연속성 로직만 검증: 첫 메시지가
`--session-id`로 새 uuid를 만들고 저장하는지, 두 번째 메시지가 저장된 uuid로 `--resume`하는지,
실패 시 세션 파일이 손상되지 않고 그대로 남는지 3가지 확인. `!새대화`의 파일 삭제와 새로
추가한 발신자 검사(권한 없는 사용자는 무조치)도 별도 확인. **실제 Discord 왕복이나 진짜
헤드리스 `claude -p` 다중 턴 대화까지 라이브로 도는 건 아직 안 함** — 다음 실제 사용 때 확인.

## 에스컬레이션 알림 연결 지점

- `cron/weekly-report.sh` — 3회 재시도 다 실패하면 `discord-notify.sh` 호출 + pending-job 기록(양방향, 답장으로 재시도 가능).
- `hooks/work-log-stop-check.sh` — 실패(타임아웃 포함) 시 `discord-notify.sh` 호출 + pending-job 기록(양방향, 답장으로 재시도 가능). 성공 시에도(LOGGED일 때만) 알림.
- `workflows/verify-task-v2.js` — `needs_clarification`(정보 부족 역질문)과 `needsUserDecision`(최대 라운드 소진) 둘 다 `discord-notify.sh` 호출 + pending-job 기록(양방향, 답장으로 재시도 가능). 전자는 답장 전체를 답변으로 붙여 재실행, 후자는 답장에서 재시도 키워드(재시도/retry/다시)만 감지해 maxRounds를 늘려 재실행.

## 2026-07-30 통합 감사 — 두 봇 사이 톱니바퀴 불일치 발견·수정

4개 병렬 조사(discord-bot.py 자체, codex-bot.py와의 드리프트, 생산자 측 스키마 일치,
launchd/config/문서 레벨)로 전체 Discord 연동을 감사. 실사용 가능한 버그부터 잠재적 결함까지
발견·수정, 나노단위로 하나씩 수정 후 코드품질(문법검사)+연동 검증(스텁 하네스로 로직 재현)을
거침. 이 문서 자체의 갱신(코덱스 봇 분리 반영)도 이 감사의 결과물이다.

- **치명적, 실사용 중 재현 가능했음**: `!코덱스`/`!코덱스대화`/`!코덱스대화초기화` 모두
  `discord-bot.py`의 자유채팅 wake-word 배제 로직(`content.startswith(CODEX_CHAT_WAKE_WORDS)`)에
  안 걸려서(그 상수는 접두어 없는 자연어 형태만 커버, `"!"`로 시작하는 명령형은 놓침) 매번
  이중 발동 — codex-bot.py가 스코프 제한된 `codex exec`로 처리하는 동시에, discord-bot.py의
  무제한 풀-툴-액세스 `claude -p` 자유채팅이 같은 텍스트를 두 번째로 처리, 같은 저장소에
  락 없이 동시쓰기가 실제로 일어날 수 있었다. `content.startswith("!코덱스")`도 함께
  체크하도록 수정.
- **높음**: `hooks/work-log-stop-check.sh`의 사용량-게이트-스킵 분기가 실제 성공 경로와
  똑같이 `exit 0`을 반환해서, `discord-bot.py`의 재시도 답장 핸들러가 "게이트에 또 막힘"과
  "진짜 시작됨"을 구분 못 하고 항상 거짓 성공 응답을 보냈다 — `weekly-report.sh`가 이미
  풀어둔 문제(전용 `exit 4`)와 동일 케이스라 같은 관례로 통일.
- **중간**: discord-bot.py의 풀-툴-액세스 `claude -p` 킬 사이트 5곳(주간보고서/verify-task-v2
  재시도 2개/자유채팅 중지·타임아웃)이 `codex-bot.py`엔 이미 적용된 SIGTERM-먼저 방식
  (`_kill_process_group_graceful`) 대신 여전히 하드 SIGKILL — mid-write 파일 손상 위험,
  전부 graceful로 전환. `handle_work_log_retry`만 `start_new_session`/프로세스그룹kill 패턴
  자체가 빠져 있던 것도 다른 4개 핸들러와 통일.
- **중간**: `codex-bot.py`의 `diff_stat`(무제한 길이)이 메시지 조합 순서상 뒤쪽의
  의도적으로 tail-슬라이스된 필드(코덱스 응답/디스클레이머)보다 앞에 와서, 1900자 최종
  절단이 앞이 아니라 뒤를 잘라야 할 내용을 대신 잘라버리는 경우가 있었음(예전에
  discord-bot.py에서 한 번 고친 것과 같은 버그 클래스가 codex-bot.py에서 재발) —
  `diff_stat` 자체를 소스에서 600자로 캡.
- **중간**: `verify-task-v2.js`의 에스컬레이션 메시지가 `needs_clarification`일 때 실제
  질문 대신 "AskUserQuestion을 직접 못 부름..." 고정 문구를 보여줬다(`reason || questions`
  순서에서 `reason`이 항상 이김) — Discord에 실제 질문이 노출되도록 수정.
- **중간**: `verify-task-v2.js`의 pending-job 작성이 서브에이전트에 자연어로만 위임되고
  반환값을 전혀 확인 안 해서, 실패해도 완전히 조용했음 — schema를 줘서 성공/실패/사유를
  구조화된 값으로 받고, 실패 시 최소한 로그에 남기도록 수정(근본적으로 LLM 지시이행에
  의존한다는 한계 자체는 남음, `docs/verify-task-v2-design.md` 참고 대상 후보).
- **낮음**: `codex-bot.py`의 `_git_output()`이 유일하게 `env=SUBPROCESS_ENV`를 안 넘기던
  subprocess 스폰 지점 — 통일. 재시도-부정 정규식(`필요.{0,2}없`)이 "필요까지는 없"류의
  더 긴 조사구를 놓치던 사각지대를 `{0,6}`으로 확장. pending-job 디렉토리에 정리 메커니즘이
  전혀 없어(48시간 지난 항목은 "답장이 왔을 때"만 검사) 무응답 에스컬레이션이 영구
  누적되던 것을, 봇 시작 시(`on_ready`) 1회 정리하는 스윕으로 보완. `verify-task-v2.js`의
  `gatherContext`가 `cwd` 존재 여부를 한 번도 확인 안 해서 정리된 스크래치패드를 가리키는
  낡은 pending-job에 답장하면 조용히 빈 컨텍스트로 계속 진행될 수 있던 잠재 결함도, 존재
  확인 후 즉시 실패하도록 수정.
- **의도적으로 안 고치고 남겨둔 것** (설계 결정이 필요하거나 범위가 더 큰 항목):
  - `CODEX_DISPATCH_LOCKS`가 프로세스 로컬이라 두 봇 프로세스 사이 진짜 크로스프로세스
    동시쓰기를 막지는 못함 — 위 wake-word 수정으로 가장 흔한 재현 경로는 닫혔지만, 사용자가
    거의 동시에 서로 다른 명령(예: `!코덱스` + verify-task-v2 답장 재시도)으로 같은 저장소를
    건드리는 드문 경우는 여전히 이론상 가능. 파일 기반 크로스프로세스 락 같은 새 공유
    프리미티브가 필요해 별도 설계 판단 대상.
  - `codex-bot.py`엔 `discord-bot.py`의 `!중지`에 대응하는 중단 명령이 없음(기능 패리티
    갭, 버그는 아님).

**Codex 독립 코드리뷰 (2026-07-30, 위 수정 완료 후) — 추가로 발견·처리한 것:**
- **[blocking → 수정됨] pending-job 답장 전체가 채널 멤버 전원에게 열려 있었음** — 위
  "의도적으로 안 고치고 남겨둔 것" 목록에 있던 항목을 사용자에게 다시 확인한 뒤 확정:
  `handle_pending_reply`를 `free_chat_user_id`(본인)로 제한. 자세한 내용은 위 "권한 경계"
  절 2026-07-30 정정 참고.
- **[high → 수정됨] pending-job이 검증 전에 삭제되던 문제** — `job_type`이 알려진 4종
  중 하나이고 `params`가 실제 dict일 때만 삭제+디스패치하도록 변경. 손상되거나 인식 못 하는
  job은 파일을 보존하고 채널에 거부/경고 메시지만 보낸다(전엔 무조건 먼저 삭제해서, 핸들러의
  `params.get(...)`이 예외를 던지면 재시도 정보가 영구 소실될 수 있었음).
- **[blocking → 수정됨] graceful kill이 손자 프로세스 생존을 못 막을 수 있었음** —
  `_kill_process_group_graceful`이 직계 자식(`proc`)의 종료만 확인하고 끝났는데, SIGTERM을
  받은 wrapper가 먼저 죽고 그 밑의 실제 write를 하던 손자(codex/agy 등)가 아직 살아있는
  경우를 못 잡았음. SIGTERM 이후 원래 pgid로 그룹 전체 생존 여부를 한 번 더 확인(signal 0
  존재 확인)하고, 남아있으면 그제서야 SIGKILL로 확실히 정리하도록 수정.
- **[medium → 수정됨] `diff_stat` 캡이 untracked 파일 많을 때 진짜 요약줄을 밀어냄** —
  같은 날 앞서 한 첫 번째 캡 수정(D1)이 놓쳤던 케이스. tracked 요약과 untracked 메모를
  각각 별도 예산으로 캡하도록 재수정(재현 테스트로 확인: untracked 60개에서도 요약줄 보존).
**낮은 우선순위 항목 처리 (2026-07-30, 뒤이은 세션에서 순서대로 완료)**:
- `verify-task-stop-check.sh`/`usage-routing-check.sh`의 fail-open 판정 — scriptPath
  basename이 정확히 `verify-task(.js)`/`verify-task-v2(.js)`/재개용 사본
  (`verify-task-wf_<runid>.js`)일 때만, Skill 이름은 정확히 일치할 때만 인정하도록 좁힘.
  **거기서 그치지 않고 자체 end-to-end 재현으로 추가 결함 발견**: `Workflow({name:
  "verify-task", ...})`처럼 `scriptPath` 대신 등록된 `name`으로 부르는 방식(docs가 명시한
  유효한 두 번째 호출법)은 아예 안 잡혔다 — `.input.name`도 함께 확인하도록 확장, 실제
  세션 트랜스크립트로 재현·수정 확인.
- `weekly-report.sh`의 Calendar/사용량 감지 정규식 — OAuth 만료/토큰 문제/MCP 서버 불가,
  429/rate limit exceeded/overloaded/quota exceeded 등 흔한 변형 추가, 배터리 테스트로 신규
  포착·기존 회귀 없음·오탐 없음 확인.
- `score-dispatch.sh`의 `CODEX_BIN`/`AGY_BIN` override가 `verify-task(-v2).js`의 preflight엔
  적용 안 되던 것 — 같은 `${VAR:-기본값}` bash 파라미터 확장 관례를 preflight 프롬프트에도
  적용해 일관성 확보.
- `CODEX_DISPATCH_LOCKS` 크로스프로세스 미보호(사용자 확정, blocking 등급) — 파일 기반
  `try_acquire_repo_lock`(`flock`, non-blocking, 즉시 거부)을 `discord_bot_common.py`에 추가,
  codex-bot.py의 `!코덱스`/`!코덱스대화`와 discord-bot.py의 verify-task-v2 재시도 2곳 전부에
  적용. 실제 별도 OS 프로세스 두 개로 mac-agent 실제 경로를 놓고 경쟁하는 시나리오까지
  재현해 확인.
- codex-bot.py `!중지` 패리티 갭(사용자 확정) — `!코덱스중지 <별칭>` 추가.
  `CODEX_CURRENT_PROCS`(별칭/정규화된 cwd별 dict — discord-bot.py의 `FREE_CHAT_CURRENT_PROC`과
  달리 여러 별칭이 동시에 돌 수 있어 dict) + 기존 `CODEX_DISPATCH_LOCKS`를 함께 확인.

## 크로스봇 채널 맥락 공유 (2026-07-30, 사용자 명시적 요청)

**요구사항 (사용자 원문 취지)**: Claude 자유채팅↔Claude, Codex 대화↔Codex 각각의 고유 세션/스레드
연속성(`--resume`/`exec resume`)은 그대로 분리 유지 — 세션을 합치자는 게 아니다. 다만 같은
Discord 채널 안에 있으니, 사용자가 Claude와 나눈 대화를 Codex도 볼 수 있어야 하고 반대로도
마찬가지여야 한다.

**구현**: `discord_bot_common.py`의 `fetch_cross_bot_context(channel, own_bot_id, limit=20)` —
`channel.history()`로 채널의 최근 메시지를 읽어 **자기 자신의 봇이 아닌** 메시지(상대 봇의
응답 + 사용자가 상대 봇에게/일반적으로 보낸 메시지 전부, 어떤 게 "진짜 상대 봇을 향한
메시지"였는지 굳이 판별하지 않음 — 판별 자체가 애매한 문제라 우회)를 시간순으로 정리해
반환. 별도 파일 브릿지나 설정 없이 discord.py가 이미 갖고 있는 채널 히스토리만 활용.

`handle_free_chat`(discord-bot.py)과 `_codex_chat_turn_locked`(codex-bot.py) 양쪽 다, 매 턴마다
이걸 호출해서 비어있지 않으면 "[참고 — 같은 채널에서 최근 다른 봇과 나눈 대화, 네 실제
세션/스레드 기록 아님, 사실로 단정하지 말고 참고만]" 문구와 함께 프롬프트 앞에 얹는다. 비어
있으면(채널에 상대 봇 흔적이 아예 없는 경우) 원본 텍스트 그대로 보내 불필요한 섹션을 안 만든다.
`!코덱스`(단발 디스패치)에는 적용 안 함 — "대화"라는 사용자의 원 표현에 맞춰 대화형 진입점
(자유채팅, `!코덱스대화`, 코덱스 wake-word)에만 적용.

`limit=20`(Discord 메시지 개수 기준, 토큰 기준 아님)으로 상한을 둬서 채널이 아무리 길어져도
프롬프트가 무한정 커지지 않는다.

**실채널 검증(2026-07-30)**: 배포 직후, 실제 채널 REST API로 과거 대화를 가져와
`fetch_cross_bot_context`에 그대로 먹여봤다 — 마침 채널에 이 기능이 필요했던 실제 사례가
그대로 남아있었다: 사용자가 "콕스야"라고 불렀는데 맥이 "콕스"라는 이름에 대한 기억이
자기 어디에도 없다고 답한 실제 기록(오늘 배포 이전 시각). 이 실제 데이터로 양방향(맥
쪽/콕스 쪽) 참고자료 생성, 자기 필터링, 시간순 정렬, 한글/코드블록 인코딩까지 전부 정상
확인. 완전한 end-to-end(사용자가 실제 타이핑 → LLM이 참고자료를 실제로 반영한 응답)는
사용자 계정으로 메시지를 보낼 방법이 없어 자동 검증은 못 함 — 다음 실사용 때 확인.

### 정체성 부여 (같은 날 후속) — 왜 필요했는지 실측으로 확인됨

위 실채널 검증 중 발견한 근본 원인: 두 봇 다 자기 자신이 누구인지, 옆에 누가 있는지에 대한
시스템 레벨 인지가 전혀 없었다 — `handle_free_chat`/`_codex_chat_turn_locked` 둘 다 사용자
텍스트(+크로스봇 참고자료)만 그대로 넘겼을 뿐, "너는 누구다"라는 페르소나가 아예 없었다.
사용자 확정 후 `discord_bot_common.py`에 `MAC_BOT_NAME`("맥")/`CODEX_BOT_NAME`("콕스",
둘 다 사용자가 이미 붙여둔 실제 Discord 봇 계정명 그대로 채택 — 새로 안 지음)과
`MAC_BOT_PERSONA`/`CODEX_BOT_PERSONA`(이름, 역할, 상대방 존재 인지, 크로스컨텍스트 블록의
성격 설명) 추가.

**주입 방식이 두 봇에서 다른 이유**: `claude -p`는 `--append-system-prompt <text>` 플래그가
있어서(확인됨: 실제 호출로 시스템프롬프트가 반영되는 것 확인) 대화 밖에서 매 턴(resume
포함) 독립적으로 다시 붙일 수 있다 — 매턴 같은 텍스트라 프롬프트 캐시에도 손해 없음. 반면
`codex exec`엔 시스템프롬프트 전용 플래그가 없다(`codex exec --help`로 확인) — 페르소나
텍스트가 곧 `prompt_text`의 일부가 되어 코덱스 자신의 스레드 히스토리에 그대로 남으므로,
매턴 반복하면 `exec resume`할 때마다 같은 자기소개가 계속 쌓여 노이즈가 된다. 그래서
새 스레드 시작(`existing_thread_id`가 없는 분기)에만 한 번 넣고, 이후로는 `exec resume`이
이어받는 스레드 자체 기억에 맡긴다.

`fetch_cross_bot_context`의 라벨링도 "[봇]" 같은 일반 표기 대신 실제 `msg.author.display_name`을
쓰도록 개선 — 페르소나가 상대를 이름으로 설명해줘도 참고자료 블록 자체가 "봇"으로만
뭉뚱그려져 있으면 다시 헷갈릴 수 있어서.

### 맥을 실제 오케스트레이터로 — 정체성 부여 직후 재수정

사용자가 곧바로 정정: "지금의 멀티에이전트[터미널 오케스트레이터가 지시받으면 판단해서
코덱스에게 능동적으로 위임하는 것]를 디스코드에서도 그대로 쓰고 싶다." 위에서 처음 쓴
`MAC_BOT_PERSONA`는 정반대 방향이었다 — 코덱스 관련 요청이면 무조건 `콕스`한테 떠넘기라고
지시했었다. 그런데 `handle_free_chat`의 `claude -p`는 애초에 인터랙티브 세션과 동일한 풀
툴 권한(Bash 포함, 저장소 제한 없음 — Phase 3 설계 자체가 그랬음)을 갖고 있어서, 터미널의
나처럼 Bash로 직접 코덱스를 부를 능력이 이미 있었다. 페르소나가 그 능력을 쓰지 말고
사람에게 떠넘기라고 지시하고 있었을 뿐.

`MAC_BOT_PERSONA` 재작성: `codex-execute-dispatch.sh <저장소경로> <지시문파일>`(verify-task-v2.js
Full track 3단계가 실제 코덱스 실행에 쓰는 것과 동일한 쓰기 가능 디스패처)를 직접 부를 수
있다고 알려주고, 코덱스 자기 보고를 그대로 믿지 말고 `git status`/`git diff`로 실행 전/후
직접 대조하라는 규율(이 저장소 전체에 이미 깔린 자기보고 불신 원칙 — score-dispatch.sh/
codex-bot.py의 before/after diff 검증과 동일)까지 명시. `콕스`를 직접 부르는 경로는 그대로
남겨둠(사용자가 명시적으로 원하면), 다만 맥이 코딩 요청을 무조건 그쪽으로 떠넘기지 않고
직접 처리할지/위임할지 상황 판단하도록 바꿈.

**검증(2026-07-30)**: 실제 `claude -p --append-system-prompt`로 두 가지 실제 시나리오를 돌려
확인 — (1) "discord-bot.py 오타 하나 고치기"에는 "직접 고칠 거야, Read/Edit이 diff 대조하며
왕복하는 것보다 빠르고 확실해"로 정확히 답함. (2) "저장소 전체 훑어서 죽은 함수 정리"에는
"코덱스한테 넘기는 게 낫다고 판단해"라고 답하며 `codex-execute-dispatch.sh`로 위임하고
`git status`/`git diff`로 직접 검증한 뒤 보고하겠다는 계획까지 정확히 설명 — 작업 규모에 따른
직접처리/위임 판단이 실측으로 확인됨.

### 콕스→맥 위임 — 대칭 요청이었지만 메커니즘은 대칭이 아님

사용자 후속 요청: "콕스야도 똑같이 위임 판단하게 해줘." 맥→콕스와 완전히 같은 방식(콕스가
Bash로 직접 `claude -p`를 부름)은 실측으로 막혔다 — `codex exec -s workspace-write` 샌드박스
안에서 `claude -p`를 직접 실행시켜보니 빈 응답 또는 90초 타임아웃이 남(scratch 저장소로
재현 확인, 2026-07-30). 네트워크 아웃바운드가 막혀있는 것으로 추정. `-s danger-full-access`로
풀면 되겠지만, 그건 파일쓰기 범위 제한이라는 `!코덱스`/`!코덱스대화`의 원래 안전장치를
없애는 거라 채택 안 함(사용자에게 옵션 제시 후 자동위임을 코덱스-분리 방식으로 선택받음).

**구현 (자동 위임, 코덱스-별도 파이썬 계층)**: `discord_bot_common.py`에
`CODEX_DELEGATE_TO_MAC_MARKER = "[위임:맥]"` 추가. `CODEX_BOT_PERSONA`에 코딩/저장소 범위를
벗어나는 요청이면 응답을 정확히 이 마커로 시작하고 뒤에 위임할 내용을 이어쓰라고 지시.
`_codex_chat_turn_locked`가 코덱스의 `reply_text`를 받은 직후 이 마커로 시작하는지 확인하고
(마커만 있고 내용이 없으면 안전하게 원래 응답 경로로 폴백), 매치되면 diff 리포트를 건너뛰고
`_delegate_to_claude()`를 호출 — 이 함수가 **코덱스의 샌드박스 밖, codex-bot.py 자신의 호스트
레벨 프로세스**에서 `claude -p`를 직접 실행한다(`--append-system-prompt`로 `MAC_BOT_PERSONA`도
동일하게 적용, `fetch_cross_bot_context`도 동일하게 적용). 맥의 영구 자유채팅 세션은
재사용하지 않고 매번 새 1회성 대화로 처리 — 두 봇 프로세스가 같은 세션 파일에 동시에
`--resume`을 시도하는 크로스프로세스 경쟁을 피하기 위함(`!코덱스`가 콕스 자신의 영구
스레드를 안 쓰고 매번 1회성인 것과 같은 이유).

**검증(2026-07-30)**: 마커 파싱 로직(정상 위임/공백 처리/빈 위임 폴백/일반 응답/마커
단어가 문장 중간에 우연히 나오는 경우) 5개 케이스 스텁 테스트 전부 통과. 실제
`codex exec -s workspace-write`로 두 시나리오 확인 — (1) "오늘 날씨 어때? 저녁 메뉴
추천해줘"(코딩과 무관한 잡담)에는 정확히 `[위임:맥]오늘 날씨 어때?...` 형식으로 응답,
(2) "README.md에 한 줄 추가"(코딩 작업)에는 위임 마커 없이 직접 처리 — 판단 자체가 실측
확인됨.

### "100% 동일" 요청 — codex-execute-dispatch.sh만으론 부족했음

사용자 후속 요청: "터미널의 너와 디스코드의 맥은 100% 동일해야해. 콕스의 역할도 마찬가지고."
위 `MAC_BOT_PERSONA`는 코덱스 위임을 `codex-execute-dispatch.sh` 직접호출로만 안내했는데 —
이건 이 저장소에서 실제 "코딩 위임"의 정식 엔진인 `verify-task-v2.js` Full track(스펙 고정→
블라인드 비평→다단계 검증→하네스 규칙 누적, `verify-task-v2-design.md` 참고)을 완전히
우회하는 얕은 버전이었다. 완전한 동일성이 아니었음.

**필요한 인프라는 이미 다 있었다**(새로 만들 필요 없음, 확인만 하면 됐음):
- `~/.claude/settings.json`의 Stop 훅(`verify-task-stop-check.sh` 등)은 유저 스코프라 별도
  `--settings` override가 없는 `claude -p` 헤드리스 호출에도 그대로 적용된다 — 즉 맥이 3개
  이상 파일을 직접 고치면 이미 인터랙티브 세션과 동일한 MANDATORY 검증 훅이 걸린다.
- `handle_free_chat`의 env는 이미 `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0`을 설정해서, 자기
  턴 안에서 `Workflow` 호출이 끝날 때까지 제대로 블록한다(`handle_verify_task_v2_retry`가
  이미 실측 검증한 것과 동일 메커니즘).
- `FREE_CHAT_TIMEOUT_SECONDS`도 처음부터 "`!코덱스`/verify-task-v2와 동일 예산"으로 30분
  잡혀 있었다(코드 주석에 이미 그렇게 적혀 있었음).

빠진 건 인프라가 아니라 **페르소나가 이 능력을 안 알려줬다는 것뿐**이었다.

**비용 트레이드오프 확인 후 사용자 확정**: Claude 계정이 이 시점 7일 창 60%로 빨간불이고
직전에 실제 세션 한도초과까지 겪은 상태라, "모든 위임을 무조건 verify-task-v2로"는 위험
크다고 판단해 사용자에게 확인 → **"작은 작업은 가볍게, 진짜 코딩 위임만
verify-task-v2로"** 확정.

**구현**: `MAC_BOT_PERSONA`를 3단 판단 기준으로 재작성 —
1. **트리비얼**(오타, 한 줄 확인): 맥이 Read/Edit/Bash로 직접 처리.
2. **소규모 위임**(파일 하나, 간단한 변경): 기존 `codex-execute-dispatch.sh` 직접호출 유지.
3. **진짜 코딩 위임**(새 기능, 여러 파일, 로직 있는 작업): `Workflow({scriptPath:
   "workflows/verify-task-v2.js", args: {task, cwd, persona}})`를 맥이 직접 호출 — 코덱스가
   스스로 계획을 세우고, 클로드/안티그래비티가 블라인드 비평하고, 반영해서 실행하고, 다시
   듀얼 코드리뷰까지 거치는 정식 파이프라인이 그대로 돈다. 파일수/민감경로 기준 경량/전체
   자동 티어링은 verify-task-v2 스스로 하므로 맥은 그냥 불러주기만 하면 됨.

`_delegate_to_claude`(콕스→맥 위임)는 별도 수정 없이 자동으로 이 개선을 물려받음 —
`MAC_BOT_PERSONA`를 그대로 import해서 재사용하는 구조라, 상수 하나만 고치면 두 경로 다
갱신된다.

**검증(2026-07-30, 계정 빨간불이라 저비용 방식으로만)**: 전체 파이프라인을 실제로 돌리지
않고, "실행하지 말고 방식만 한 단어로 답해" 프롬프트로 판단 로직만 확인 — 3개 요청("오타
고쳐줘"/"README 섹션 추가"/"pending-job 정리 대시보드 새로 만들어줘, 파일 여러 개 걸쳐도
됨")에 정확히 "직접 처리 / codex-execute-dispatch.sh / Workflow+verify-task-v2"로 분류됨.

### 계정 한도 = provider 개인 문제, 시스템 전체 장애 아님 — 자동 provider 폴백

사용자 지적: "맥은 실제 계정을 따라가지 않고, 멀티에이전트를 따라가야 하는데 잘못 판단하고
있어." 맥이 자기 계정(Claude) 한도로 응답을 못 하면 그냥 "기다리세요"로 끝냈는데, 이건
시스템 전체가 죽은 것처럼 취급하는 단일장애점식 사고다 — 이 저장소 다른 곳
(`route-dispatch.sh` Rule B: "단순 작업은 안티그래비티 먼저 시도, 실패하면 코덱스로
폴백")에는 이미 있는 멀티에이전트 폴백 원칙이 맥/콕스의 실패 처리에는 안 붙어 있었다.

**구조적 제약**: 맥 자신이 `claude -p` 프로세스이므로, Claude API 호출 자체가 한도로 막히면
맥 "내부"에서 다른 provider로 갈아탈 수 없다(맥의 사고 자체가 그 API 호출이므로) — 폴백은
반드시 감싸는 파이썬 코드(discord-bot.py/codex-bot.py 자신, 각 봇의 claude-p/codex-exec
호출 밖)가 해야 한다.

**비용 확인 후 사용자 확정**: "discord-bot.py가 자동으로 코덱스로 재시도"(사전 사용량
비교로 미리 우회하는 방식이 아니라, 실패가 실제로 감지된 뒤에 재시도).

**구현**:
- `discord_bot_common.py`에 `QUOTA_LIMIT_PATTERN`(weekly-report.sh의 한도 감지 정규식을
  파이썬으로 옮긴 공유 상수)을 추가해 discord-bot.py/codex-bot.py가 같은 기준으로 "이건
  코드 버그가 아니라 계정 한도"를 판정하도록 통일.
- `discord-bot.py`: `handle_free_chat`의 두 감지 지점(사전 `usage_gate_check` 게이트 / 실행
  후 실패 문구 매칭) 모두에서 `_fallback_to_provider_chain(message, text)`를 호출 — 같은
  메시지를 먼저 `agy --print --mode plan`으로 전달하고, Antigravity가 usable하지 않을 때만
  Codex 7일창 게이트 후 `codex exec -s read-only -C <FREE_CHAT_CWD>`로 재시도한다. 이
  폴백은 읽기/응답 전용이며 실제 파일 수정은 하지 않는다. 각 provider의 현재 process를
  `FREE_CHAT_CURRENT_PROC`에 등록하므로 `!중지`가 실제 실행 중인 톱니를 종료하고 다음
  provider 진행도 막는다. 성공한 대체 응답은 다음 Claude 턴의 bounded 참고자료로 연결한다.
  응답에는 실제 엔진 라벨을 붙이고, 둘 다 실패하면 두 provider 진단을 함께 안내한다.
- `codex-bot.py`: `_codex_chat_turn_locked`의 실패 분기에서 `raw` 출력이
  `QUOTA_LIMIT_PATTERN`에 매칭되면 기존 `_delegate_to_claude()`(콕스→맥 위임 마커 처리에
  이미 쓰던 헬퍼)를 그대로 재사용해 맥으로 폴백 — "판단해서 위임"이든 "실패해서 폴백"이든
  맥에게 필요한 지시는 결국 같으므로 새 함수 불필요.
- **의도적으로 범위 밖에 둔 것**: `!코덱스`(one-shot 쓰기 디스패치)는 이 폴백 대상에서
  제외 — 그 경로는 이미 "재시도 없음(의도적)" 설계 결정이 있고(부분 변경 누적 위험, 위
  섹션 참고), 실패 후 *다른 엔진*으로 넘기면 그 위에 또 다른 쓰기가 얹히는 것이라 리스크
  성격이 다르다. 이번 폴백은 "아직 아무것도 확정적으로 안 써진" 채팅 응답 실패에만 적용.

**검증(2026-07-30, 스텁 시뮬레이션 — 실제 codex exec는 이미 여러 번 같은 패턴으로
검증했으므로 재호출 안 함)**: 5개 시나리오 전부 통과 — 사전게이트 폴백 성공 / 사후실패
폴백 성공 / 한도와 무관한 일반 버그는 폴백 안 하고 원래 에러 유지(신뢰검사 버그 같은 실제
코드결함과 안 섞이는 것 확인) / 맥·코덱스 둘 다 한도일 때 이중실패 안내까지 정상 도달 /
정상 응답 회귀 없음. 실제 겪은 에러 원문("You've hit your session limit...")으로
`QUOTA_LIMIT_PATTERN` 매칭도 재확인.

### 후속 정정 — "터미널처럼 처리하게 하면 안 돼?" 질문으로 발견한 실제 원인

사용자 후속 질문: "맥은 지금 6200계정으로 나와 터미널에서 대화하고 있잖아. 그런데 왜
디스코드에서는 한도가 막혔다고 나오는 것인지 모르겠어." 위에서 "새 프로세스라서 다르게
처리될 수 있다"고 한 설명은 검증 없는 추측이었다 — 실제 트랜스크립트 데이터로 재확인.

**실측(2026-07-30)**: 터미널 세션(`9ca469bb-...`)의 `usage` 필드를 직접 대조한 결과,
문제의 실패 시각(15:04:35 UTC)에 터미널 세션은 8분 반째(14:56:06~15:04:44) 아무 요청도
안 보내고 있었다 — "터미널은 활발히 동작 중이라 안 막혔다"는 앞선 설명 자체가 사실과
달랐다. 디스코드 맥의 실패 호출은 응답 토큰 0(요청이 시작도 못 하고 거부됨)이었고,
**터미널 세션의 바로 다음 요청은 단 9초 뒤(15:04:44)에 캐시적중률 100%로 정상 통과**했다.
즉 계정 전체가 막힌 게 아니라, 그 순간 그 요청 하나에만 걸린 몇 초 단위의 순간적 순단이었다
— "새 프로세스 vs 이어지는 세션"이라는 구조적 차이가 원인이 아니었다(캐시적중률도 두 세션
다 정상 범위였음, 별도로 확인).

**결론**: "디스코드를 터미널처럼 처리한다"는 방향 자체가 틀린 진단이었다 — 애초에 구조적으로
"다르게 처리"되는 지점이 없다. 대신 실측이 알려준 진짜 유용한 수정: 9초 만에 자연 회복됐다는
사실 자체가 "코덱스로 넘기기 전에 짧게 한 번 더 같은 Claude로 재시도"가 훨씬 정확한 대응이라는
근거가 됨.

**구현**: `handle_free_chat`에 짧은 재시도 계층 추가 — 사후 실패(claude -p 실행 후
`QUOTA_LIMIT_PATTERN` 매칭)에서만 적용(사전 `usage_gate_check` 게이트는 순간적 순단이 아니라
더 느리게 움직이는 집계 지표라 이 재시도가 의미 없음, 그대로 즉시 코덱스 폴백 유지).
`CLAUDE_QUOTA_RETRY_DELAY_SECONDS = 10`(실측 9초 간격에 약간의 여유를 더한 값, 정밀한
과학적 근거는 아님) 대기 후 같은 `claude -p` 호출(같은 `--resume`/세션 상태)을 1회 재시도 —
재시도가 성공하면 코덱스로 안 넘어가고 정상 응답. 재시도도 한도 문구로 실패해야만(진짜 7일창
소진 등 더 오래가는 문제) 기존 코덱스 폴백으로 넘어감. 스폰+통신 시퀀스를 `_run_once()`
내부 클로저로 뽑아 원본 시도/재시도가 같은 로직(타임아웃 처리, `FREE_CHAT_CURRENT_PROC`
갱신 포함)을 정확히 공유하도록 함.

**검증(2026-07-30, 스텁)**: 4개 시나리오 전부 통과 — 1차 한도초과→재시도 성공(코덱스 안
감, 정확히 2회만 호출) / 1차·재시도 둘 다 한도초과→코덱스 폴백(정확히 2회 호출 후 폴백) /
한도와 무관한 일반 버그→재시도 없이 바로 에러(1회만 호출, 코덱스도 안 감) / 1차 정상 성공→
재시도 없음(1회만 호출).

### 실채널 테스트로 발견한 별개 버그 — `codex exec resume`의 신뢰 검사 실패

사용자가 "맥아"/"콕스야"로 실제 채널에서 테스트하다가 "❌ 코덱스 실행 실패 (exit=1)... Not
inside a trusted directory and --skip-git-repo-check was not specified." 에러를 재현함 —
**오늘 세션에서 새로 만든 게 아니라 원래부터 있던 버그**(실제 과거 채널 히스토리에도 동일
에러가 반복 기록돼 있었고, 사용자가 매번 `!코덱스대화초기화`로 우회하고 있었음).

**근본 원인**: `codex exec resume <thread_id>`엔 `-C`/`--cd` 플래그가 아예 없다(`codex exec
resume --help`로 확인) — 그런데 신뢰 검사는 여전히 프로세스의 실제 OS `cwd`를 본다.
`codex-bot.py`는 `com.macagent.codex-bot.plist`에 `WorkingDirectory`가 지정 안 돼 있어 실제
프로세스 cwd가 `/`다(`lsof -p <pid> | grep cwd`로 확인) — `/`는 신뢰된 프로젝트 목록
(`~/.codex/config.toml`의 `[projects."..."]`)에 없으므로 매 resume 턴마다 실패. 새 스레드
시작 경로(`-C <trusted dir>`를 명시하는 쪽)는 우연히 이 문제를 안 겪어서 지금까지 안 잡혔다.

**재현**: `cd / && codex exec resume <실제 thread_id> --json -- "test"` → 100% 재현
(exit=1, 정확히 사용자가 본 에러 문구와 동일). `--skip-git-repo-check` 추가 후 재실행 →
정상 응답(exit=0) — 수정 전/후 대조로 확인.

**수정**: `_codex_chat_turn_locked`의 resume 분기에 `--skip-git-repo-check` 추가. resume은
이미 확립된 스레드를 이어가는 것뿐이라 git 저장소 경계를 새로 검증할 필요가 없다는 점에서
안전한 우회 — 새 스레드 시작 경로(`-C <trusted dir>` 명시)는 그대로 둠.

### 응답 속도 개선 — 실제 지연시간 측정 후 두 가지 원인 수정

사용자 지적: "답변이 너무 느린거 같은데... 그냥 부른건데 너무 느리잖아." 실제 Discord
메시지 타임스탬프를 직접 대조해 확인(추측 아님) — "콕스야" → "응, 콕스 여기 있어."(한 줄
인사)에 두 번 모두 **19.5~19.8초**, "맥아" → 맥의 응답에 **58초**가 걸렸다.

**원인 1 — 콕스**: `~/.codex/config.toml`의 전역 `model_reasoning_effort = "high"`가 캐주얼한
채팅 응답에도 그대로 적용되고 있었다. 코딩 작업(`!코덱스`, verify-task-v2 위임)엔 맞는
설정이지만, 채팅/wake-word 경로엔 과함. 사용자 확정("낮춤(medium으로) — 채팅/wake-word만"):
`_codex_chat_turn_locked`(대화형 채팅/wake-word 경로)의 두 `codex exec` 호출(resume/신규
스레드) 모두에 `-c 'model_reasoning_effort="medium"'` 오버라이드 추가 —
`CODEX_CHAT_REASONING_EFFORT_ARGS` 공유 상수로 관리. `!코덱스`(`codex-execute-dispatch.sh`
경유)와 verify-task-v2 위임은 건드리지 않음(전역 high 그대로).

같은 조건(동일 프롬프트, 동일 scratch 저장소)으로 직접 시간 측정: high=5.3초,
medium=4.1초 — 실제로 더 빠르지만, 라이브에서 관측된 19.5초와는 격차가 크다. 정직하게
기록: reasoning effort는 실제 기여 요인이지만 유일한 원인은 아니었을 가능성이 큼 —
`usage_gate_check` 서브프로세스, `fetch_cross_bot_context`(Discord API 왕복),
`_dirty_snapshot` 전/후 2회(저장소 git status/해시 계산) 등 파이프라인의 다른 오버헤드가
나머지를 차지할 것으로 추정되나, 이번엔 추가로 분해 측정하지 않음(사용자가 확정한 범위
밖).

**원인 2 — 맥**: 58초 지연은 reasoning-effort 설정과 무관(`claude -p`엔 그런 플래그 없음)
— 실제로는 응답 내용 자체가 원인. 트리비얼한 "맥아" 인사에, 크로스컨텍스트로 곁들여진
예전 오류 대화(신뢰검사 버그 논의)를 맥이 먼저 나서서 분석·설명하는 긴 응답을 만들어냈다
— 앞서 "소소한 튜닝 이슈"로 미뤄뒀던 문제가 지연시간 문제로 재발한 것. 사용자 확정("바꿈
— 트리비얼한 인사는 짧게"): `MAC_BOT_PERSONA`에 "단순 호출/인사에는 짧게만 답하고,
크로스컨텍스트에 예전 오류/논쟁이 있어도 사용자가 다시 묻지 않는 이상 먼저 분석하지 말라"는
지침 추가.

**검증(2026-07-30)**: 콕스 쪽은 `-c` 오버라이드가 실제로 반영되는 것을 CLI 출력의
`reasoning effort: medium` 헤더로 직접 확인. 맥 쪽은 문제가 실제로 재현됐던 그 상황(예전
오류 대화가 크로스컨텍스트로 붙은 채 "맥아")을 그대로 재현해서, 수정 후 "맥이야, 왜
불렀어?"로 짧게 응답하는 것을 실제 `claude -p` 호출로 확인(수정 전엔 긴 기술 분석문이
나왔던 바로 그 조건). 두 봇 프로세스 재기동 확인.

### 남은 콕스 지연 추가 조사 — `coach`의 순차 provider 조회가 진짜 병목이었음

사용자 요청: "콕스 남은 지연도 파봐줘." `codex-bot.py`를 venv 파이썬으로 직접 import해서
`_codex_chat_turn_locked`의 각 단계(`usage_gate_check`, `_dirty_snapshot`, `_git_output`,
`fetch_cross_bot_context`)를 실제 함수로 개별 실측.

**결과**: `_dirty_snapshot`(전/후 2회 합쳐 0.04초)과 `fetch_cross_bot_context`(0.32초)는
범인이 아니었다 — 애초에 mac-agent 저장소에 untracked 파일이 없어서(`git status --porcelain`
결과 0건) 파일 해싱 경로 자체가 실행 안 됨. 진짜 범인은 `usage_gate_check("codex")` —
**5.98초**. `usage-preflight-gate.sh` → `coach --json` → `coach`의 `gather()` 함수를 직접
읽어보니, provider 필터 없이 부르면 claude/codex/antigravity **세 개를 순차 for 루프로**(병렬
아님) 각각 `codexbar` 서브프로세스로 조회 — `codexbar` 단발 호출이 ~1.5~2초라 3개 곱하면
그대로 게이트 지연이 됨. `coach --json`은 이미 `--providers` 옵션을 지원하는데,
`usage-preflight-gate.sh`가 그 옵션을 안 쓰고 있었다.

**실측 대조**: `coach --json`(3개 전부)=6.3~6.4초, `--providers codex` 하나만=1.9초,
`--providers claude` 하나만=1.8초.

**수정**: `usage-preflight-gate.sh`의 `$ACTOR`(claude/codex/dual)에 따라 `--providers`를
정확히 필요한 것만 넘기도록 — `claude`→`claude`, `codex`→`codex`, `dual`→`claude,codex`
(antigravity는 이 게이트가 애초에 안 씀, 기존 주석 그대로). 이 스크립트를 쓰는 다른
호출자(`weekly-report.sh`, `work-log-stop-check.sh`, `kakao-morning-briefing.sh`,
`discord-bot.py`의 `usage_gate_check("claude")`)도 전수 확인 — 전부 `claude`/`codex`만
쓰고 `dual`은 실사용 없음, 그대로 다 이 최적화의 수혜자.

**검증**: 수정 후 `codex`=1.92초, `claude`=1.39초, `dual`=3.47초로 재측정, 실제 판정
결과(claude=`SKIP: ... 잔여 56%(red)`, codex=`PROCEED`)도 정확히 그대로 유지되는 것까지
확인(로직 회귀 없음). `usage_gate_check("codex")` 재측정: 5.98초 → **2.21초**.

**최종 추정치**(각 단계 실측값을 그대로 합산): `usage_gate_check` 2.21초 + `_dirty_snapshot`
0.04초 + `fetch_cross_bot_context` 0.32초 + `codex exec`(reasoning=medium) ~4.1초 ≈
**~6.7초** — 원래 라이브 실측치 19.5~19.8초에서 약 66% 단축 추정(전체 파이프라인을
Discord 왕복까지 다시 라이브로 재본 건 아님).

### wake word가 문장 맨 앞에만 반응하던 버그 — "안녕 콕스"가 어느 쪽에도 안 감

사용자가 실제 Discord에서 "안녕 맥" 3초 뒤 "안녕 콕스"를 보냈는데, 콕스는 응답이 없었고
맥은 "이전 메시지를 아직 처리 중입니다" 락 충돌 메시지를 보냈다("맥은 이상하고, 콕스는
답변을 안해"). Discord REST API로 실제 채널 히스토리를 `curl`로 직접 조회해 타임스탬프로
재현: `16:28:58` "안녕 맥" → `16:29:01` "안녕 콕스" → `16:29:01.528` 맥의 락 충돌 메시지 →
`16:29:20.401` (19초 뒤) "안녕 맥"에 대한 맥의 실제 응답.

**원인**: wake word 감지가 `content.startswith(CODEX_CHAT_WAKE_WORDS)` — 문장 **맨 앞**에
이름이 와야만 매치. "안녕 콕스"는 "안녕"으로 시작하므로 (1) `codex-bot.py`의 라우팅
조건(`on_message`)에 안 걸려서 콕스에게 아예 안 들어갔고, (2) 같은 이유로
`discord-bot.py`의 배제 조건에도 안 걸려서 맥의 자유채팅 catch-all로 잘못 흘러들어갔다 —
마침 그 3초 전 "안녕 맥"을 처리 중이던 자유채팅 락과 충돌해 락 메시지가 뜬 것. 두 증상이
사실 하나의 원인.

**수정**: `discord_bot_common.py`에 `is_codex_wake_word(content)` 공유 헬퍼를 새로 추가 —
문장의 **첫 토큰 또는 마지막 토큰**이 wake word(호격 조사 야/아/씨/님, 후행 구두점 제거
후)와 일치하면 True. 단순 substring(`in`) 체크는 채택 안 함 — "어제 콕스가 이상했어"처럼
문장 중간에 콕스를 언급만 하는 경우까지 주소 지정으로 오인해 잘못 라우팅하게 되기 때문(첫/끝
토큰만 보므로 이 경우는 계속 False로 유지). `discord-bot.py`의 배제 조건(`on_message`)과
`codex-bot.py`의 라우팅 조건(`on_message`) 양쪽 모두 이 하나의 함수로 교체 — 기존 주석이
이미 경고하던 "두 파일에서 로직이 갈라지면 두 봇이 동시에 응답하거나 둘 다 응답 안 하는"
위험을 원천 차단.

**검증**: venv 파이썬으로 `is_codex_wake_word` 12개 케이스 직접 실행 — 버그 재현
케이스("안녕 콕스"→True), 회귀 케이스(맨 앞 wake word 계속 True, "!코덱스" 계열 명령 계속
False, 문장 중간 언급 계속 False) 전부 기대값과 일치 확인. 두 봇 프로세스 재기동 후
stderr.log에서 정상 `Shard ID None has connected to Gateway` 로그로 재연결 확인(import/문법
오류 없음).

### "각자 자기소개 해줘" — 이름을 안 불러도 단체방처럼 콕스도 같이 응답하게

wake word 버그를 고친 직후, 사용자가 실제로 "각자 자기소개 해줘"를 보냈다. 맥은 응답했지만
콕스는 반응이 없었고, 맥 스스로도 "콕스는 별도 봇이라 '콕스야'로 따로 불러야 한다"고
안내했다. 사용자 피드백: "각자"라는 말을 그냥 일반 단체방처럼 콕스도 이해하고 같이 말하면
되지 않아? — 매번 이름을 불러야 하는 게 아니라, 여러 참가자를 동시에 지칭하는 표현이면
콕스도 (맥과 별개로) 알아서 같이 응답해야 한다는 것.

**설계**: `is_codex_wake_word()`와는 별개 함수(`is_group_address()`)로 구현 — wake word는
"콕스 하나를 이름으로 지목"이라 첫/끝 토큰만 보지만, 그룹 지칭은 "여러 명을 한꺼번에
지칭"이라 문장 어디에 있어도 신호가 된다(단순 substring 체크). 감지 방식은
AskUserQuestion으로 확정: 좁은 목록(각자/둘 다/둘다/다같이/같이/모두 다)과 넓은 목록(+모두/
전부) 중 사용자가 넓은 쪽을 선택 — "모두 감사합니다"처럼 콕스와 무관한 문장에도 콕스가
응답할 오탐 가능성을 사용자가 명시적으로 감수하기로 확정.

**구현**: `discord_bot_common.py`에 `GROUP_ADDRESS_WORDS`/`is_group_address()` 추가.
`codex-bot.py`의 `on_message`에 `elif is_group_address(content): await
handle_codex_chat_wake(message)` 한 줄만 추가 — `discord-bot.py`의 배제 조건은 건드리지
않음(그룹 지칭 단어는 `is_codex_wake_word`에 안 걸리므로 맥은 원래대로 계속 응답, 결과적으로
단체방처럼 둘 다 답하게 됨).

**검증**: venv 파이썬으로 `is_group_address` 8개 케이스 직접 실행 — 목록에 있는 6개 단어
전부 True(오탐 케이스 3개 포함, 의도된 동작), wake-word 단독 케이스는 False로 확인. 두 봇
프로세스 재기동 후 정상 재연결 로그 확인.

**후속 버그(같은 날, 배포 직후 실사용으로 발견)**: 사용자가 "둘  다 소개 좀 해줘"(둘/다
사이 스페이스 두 칸)를 보냈는데도 여전히 콕스만 무응답, 맥은 또 "콕스야로 따로 불러야
한다"고 답함 — "이건 아니지 않아? 각자 답변해줘야지". `is_group_address`를 정확히 같은
문자열로 재현해서 원인 확인: `GROUP_ADDRESS_WORDS`의 "둘 다"/"모두 다"는 리터럴 한 칸
스페이스라, 실제로 온 두 칸 스페이스 입력과 매칭 실패("둘다"에도 안 걸림 — 공백이 0개가
아니라 2개라서). 다중 단어 항목은 전부 이 문제에 취약. **수정**: 매칭 전에
`" ".join(content.split())`로 공백을 정규화 — 개별 항목의 스페이스 개수를 맞추는 대신 입력
쪽을 정규화하는 쪽을 택함(항목이 늘어나도 재발 안 하도록). 재현 문자열 그대로 포함해 6개
케이스 재검증, 전부 통과. 두 봇 재기동 확인.

### "왜 한 명이 둘 다 소개하는거야" — 상대 봇을 대신 소개해버리던 문제

공백 정규화 수정 배포 직후 사용자가 "둘 다 소개해줘"를 다시 테스트. 실제 채널 히스토리로
확인한 타임라인:
- `17:05:49.793` 사용자: "둘 다 소개해줘"
- `17:05:59.208` 콕스: 자기소개 **+ 맥 소개까지 한꺼번에** 응답("- **콕스(저)**: ... - **맥**:
  ...")
- 맥은 이 시점까지 응답 없음(처리 중)
- `17:06:10.730` 사용자: "둘다 소개해줘"(첫 요청이 하도 안 끝나서 재입력한 것으로 보임)
- `17:06:11.023` 맥: "이전 메시지를 아직 처리 중입니다" 락 충돌 메시지
- `17:06:12.189` 맥: 첫 "둘 다 소개해줘"에 대한 응답 완료(23초 소요) — "이미 위에서
  소개해드렸어요 ... 콕스도 방금 자기소개 했네요, 맞아요. 왜 따로 소개하는 거야?"

사용자 피드백: "아니 왜 한명이 둘다 소개하는거야" — 콕스가 자기소개 요청에 맥까지
대신 설명해버린 게 문제. 그룹 지칭(`is_group_address`) 라우팅 자체는 정상 동작했지만(콕스도
독립적으로 응답은 함), 응답 **내용**이 상대 봇을 대신 말해버려서 마치 봇 하나가 둘을 다
대변하는 것처럼 보인 것.

**원인**: `CODEX_BOT_PERSONA`/`MAC_BOT_PERSONA` 둘 다 상대 봇의 존재와 역할을 설명해주는
문구가 있고, `fetch_cross_bot_context`로 상대의 최근 대화까지 참고자료로 곁들여지다 보니,
"둘 다 소개해줘"처럼 "둘 다"라는 단어가 그대로 들어간 요청을 받으면 모델이 "친절하게" 상대
몫까지 요약해서 답해버렸다 — 페르소나에 "네 얘기만 해"라는 명시적 금지가 없었던 게 근본
원인.

**수정**: 두 페르소나 모두에 대칭으로 지침 추가 — "'둘 다'/'각자'/'모두'처럼 여럿을 한꺼번에
지칭하는 요청을 받아도, 상대를 대신해서 소개하거나 의견을 답하지 마, 네 얘기만 해. 상대는
같은 요청에 별도로, 독립적으로 응답해." 두 봇 프로세스 재기동으로 반영 확인(실제 대화
재테스트는 사용자 다음 실사용 확인 필요 — persona 변경은 코드 로직이 아니라 프롬프트라
단위 테스트로 검증 불가).

락 충돌(23초 처리 중 재입력)은 별개 증상으로 판단, 이번엔 손대지 않음 — 맥의 free-chat이
풀 툴 권한 + 크로스컨텍스트를 갖고 도는 구조상 원래도 콕스보다 느리다는 건 기존
"응답 속도 개선" 절에서 이미 규명됐고, 사용자가 이번에 지적한 핵심은 지연이 아니라 응답
내용(상대를 대신 소개)이었다.

### 위 페르소나 수정이 실제로는 반영이 안 됐던 이유 — 새 스레드에만 주입되는 구조

바로 다음 실사용 테스트("안녕? 각자 소개 좀 해줄래")에서도 콕스가 여전히 맥을 대신
소개했다("맥: 참고 대화상 Claude 기반 에이전트로..."). 사용자 피드백: "아직 각자 답하지
않고 있어. 개별적으로 답해야지."

**원인**: `_codex_chat_turn_locked`의 기존 설계(2026-07-30 앞선 사용자 명시적 요청 — "코덱스
스레드는 세션 병합 아님, 정체성은 새 스레드 시작 시점에만") 상 `CODEX_BOT_PERSONA`는 **새
스레드 첫 턴에만** 프롬프트에 들어간다 — resume되는 기존 스레드는 그 지침을 아예 본 적이
없다. 이 채널의 코덱스 대화는 이미 오늘 세션 초반에 스레드가 만들어져 계속 resume되고
있었으므로, 방금 페르소나에 추가한 "상대를 대신 소개하지 마" 지침은 봇 재기동으로도 절대
반영될 수 없었다(재기동은 프로세스만 바뀔 뿐, 저장된 `thread_id`와 그 스레드의 코덱스 쪽
대화 기록은 그대로 유지). 맥 쪽은 반대로 `--append-system-prompt`로 매 턴(resume 포함)
페르소나를 새로 넣으므로 즉시 반영됐다 — 그래서 이번 라운드는 맥은 자기 얘기만 하고 콕스만
계속 상대를 소개하는 비대칭이 나타났다.

**수정**: 스레드 신규/resume 여부와 무관하게, **이번 턴의 텍스트가 `is_group_address()`에
걸리면 매번** 짧은 리마인더 문구를 프롬프트 맨 앞에 재주입하도록 `_codex_chat_turn_locked`
수정. 스레드 히스토리에 매번 쌓이긴 하지만, 그룹 지칭 요청 자체가 매번 새로 발생하는
것이므로 노이즈보다 정확성을 택함(기존 CODEX_BOT_PERSONA 전체 재주입과 달리 이건 한두
문장짜리라 축적 비용이 작음).

**검증**: 코덱스 봇 프로세스 재기동 후 정상 재연결 확인. persona/prompt 텍스트 변경이라
단위 테스트로 검증 불가 — 사용자의 다음 실사용 확인 필요.

## 알려진 제약

- launchd로 상시 구동되는 프로세스라 PATH가 `/usr/bin:/bin:/usr/sbin:/sbin` 기본값으로 축소돼 있음 — `discord-bot.py`가 `weekly-report.sh`를 서브프로세스로 띄울 때 `SUBPROCESS_ENV`로 `/opt/homebrew/bin`, `~/.local/bin`을 명시적으로 앞에 붙여서 넘김. 이 PATH 문제는 이 레포 전체에서 반복 발생한 것(tmux/coach/claude/ffmpeg/whisper-cli/codex와 동일 원인) — 새 서브프로세스 스폰 지점을 추가할 때마다 재확인할 것.
- CAPTCHA·비밀번호 확인 등 Discord 개발자 포털의 본인 인증 단계는 브라우저 자동화로 우회 불가 — 봇 앱 생성/토큰 재발급은 항상 사람이 직접.
