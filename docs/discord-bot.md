# discord-bot.py + discord-notify.sh (일정비서 — Discord 연동)

Phase 1 (사용자 요청, 2026-07-26): 온디맨드 트리거 + 일방향(Mac→Discord) 실패/에스컬레이션 알림. Phase 2 v1(사용자 요청, 2026-07-28): `weekly-report.sh` 실패 알림에 답장하면 재시도. Phase 2.5(사용자 요청, 2026-07-28): `work-log-stop-check.sh` 답장 재시도 + `verify-task-v2.js`의 `needs_clarification`(정보 부족 역질문)과 `needsUserDecision`(최대 라운드 소진) 둘 다 답장 재시도 — 후자는 자유텍스트 3지선다를 그대로 해석하지 않고 재시도 의도 키워드(재시도/retry/다시)만 감지하는 방식으로 해결(사용자 확정, 2026-07-28). Phase 3(사용자 요청, 2026-07-29): 본인(free_chat_user_id) 전용 자유 채팅 — 접두어 없이 전부 릴레이, 전체 도구 허용, `--resume`로 세션 연속성 유지(`!새대화`로 초기화).

## 구성

- `bin/discord-bot.py` — 상시 구동 프로세스(Discord Gateway WebSocket 연결). `~/Library/LaunchAgents/com.macagent.discord-bot.plist`로 launchd 상시 등록(`KeepAlive: true`, `RunAtLoad: true`) — 주간보고서처럼 주기 실행이 아니라 항상 떠 있어야 함. `~/.claude/discord-bot/venv`(격리된 venv, `discord.py 2.7.1`)로 실행. 코드를 고치면 재기동 필요: `launchctl kickstart -k gui/$(id -u)/com.macagent.discord-bot`.
- `bin/discord-notify.sh <message>` — 봇 프로세스와 무관하게 Discord REST API로 메시지 한 번 보내는 헬퍼. 실패해도 항상 exit 0 — 알림 실패가 호출한 스크립트(주간보고서 등)를 절대 죽이면 안 됨. Phase 2부터 성공 시 게시된 메시지의 Discord id를 stdout으로 반환(실패 시 빈 문자열) — 호출한 스크립트가 그 id로 pending-job을 기록해 나중에 답장을 매칭할 수 있게 함.
- 설정: `~/.claude/discord-bot/config.json` (`{"token":..., "channel_id":..., "free_chat_user_id":...}`) — 이 레포 밖, `chmod 600`. 토큰은 Discord 개발자 포털(https://discord.com/developers/applications)에서 발급, "Message Content Intent"를 반드시 켜야 봇이 메시지 내용을 읽음. `free_chat_user_id`는 Phase 1부터 미리 넣어둔 값이었고, `!코덱스`(2026-07-28)에서 처음 참조하기 시작했으며 Phase 3(2026-07-29, 자유 채팅)에서도 그대로 재사용한다 — "Claude/코덱스에게 임의 지시를 내릴 수 있는 사람"이라는 같은 권한 레벨을 의미.
- `~/.claude/discord-bot/free-chat-session.json` — Phase 3 세션 상태(레포 밖, git 추적 안 함). `{"session_id": <uuid>, "last_used_at": ISO시각}` 하나만 담는다 — 채널당 자유 채팅 사용자가 한 명뿐이라 여러 대화를 구분할 필요가 없음. `!새대화`로 삭제하면 다음 메시지가 새 세션을 시작한다.
- `~/.claude/discord-bot/pending/<message_id>.json` — Phase 2 pending-job 저장소(레포 밖, git 추적 안 함). 에스컬레이션을 쏜 스크립트가 `discord-notify.sh`가 반환한 message id로 기록: `{"type":"weekly-report-retry"|"work-log-retry"|"verify-task-v2-retry"|"verify-task-v2-decision-retry","created_at":ISO시각,"params":{...}}` — `weekly-report-retry`는 `params`가 비어있고(자기완결 스크립트라 외부 상태 불필요), `work-log-retry`는 `params`에 `session_id`/`transcript_path`를, `verify-task-v2-retry`는 `params`에 `task`(원본 전체, 자르지 않음)/`cwd`/`persona`/`maxRounds`/`historyFile`/`harnessFile`/`questions`를, `verify-task-v2-decision-retry`는 `questions`만 빼고 동일한 필드를 담는다(각각 재실행에 필요한 상태를 스크립트/워크플로우 자체가 못 들고 있어서). `discord-bot.py`가 답장을 받으면 `message.reference.message_id`로 이 파일을 찾아 `type`에 따라 디스패치하고 처리 후 즉시 삭제(중복 답장 방지). 48시간 지난 항목은 만료 처리. `type`을 못 알아들으면(향후 미구현 소스 등) 조용히 로그만 남기고 무시 — 스키마가 깨지지 않고 확장 가능.

## 권한 경계 (사용자가 명시적으로 결정한 것, 재검토 없이 그냥 넓히지 말 것)

- **인가된 채널만**: `config.json`의 `channel_id` 하나만 반응, 그 외 채널/DM/다른 서버/봇 자신의 메시지는 전부 무시. 이게 신뢰 경계 전부 — 그 채널은 초대받은 사람 전원이 완전히 신뢰된다는 사용자의 명시적 결정.
- **자유 채팅(Claude Code에 임의 지시)은 본인만**: Phase 3(2026-07-29)에서 실제로 구현됨 — `free_chat_user_id`와 발신자 id가 일치할 때만, 채널 내 다른 사람에게는 안 엶(사용자가 Phase 1 때부터 이미 결정해둔 것 그대로). 온디맨드 트리거/에스컬레이션 응답과 분리된 별도 권한 레벨.

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

## `!코덱스` — 코덱스 직접 디스패치 (본인 전용, 2026-07-28)

```
!코덱스 <저장소별칭> <작업 지시>
```
예: `!코덱스 mac-agent discord-bot.py에 로그레벨 옵션 추가해줘`

지금까지 디스코드가 실행시키는 건 전부 `claude -p` 경로(주간보고서 등)뿐이었는데, 코덱스로
가는 길이 없었다. 이 명령어로 디스코드에서 코덱스에게 직접 코딩 작업을 맡길 수 있다.

- **재사용**: `workflows/lib/codex-execute-dispatch.sh <cwd> <prompt-file>`를 그대로
  사용(verify-task-v2용으로 이미 있던 write-capable 코덱스 실행기, 수정 없음) —
  `codex exec --skip-git-repo-check -s workspace-write -C <cwd> "$(cat <prompt-file>)"`를
  실행하고 `{"ok": bool, "message": string}` JSON을 반환한다.
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
- **커밋/푸시 안 함**: diff까지만 보여주고 끝 — 커밋·푸시는 별도의 명시적 요청이 있을 때만
  (되돌리기 어려운 작업이라 자동화하지 않음).
- **재시도 없음(의도적)**: `weekly-report.sh`와 달리 자동 재시도가 없다 — 코딩 작업은
  실패 후 재실행하면 부분 변경이 누적될 수 있어, 실패하면 결과(+diff)만 보여주고 사용자
  판단에 맡긴다.
- **저장소별 동시 실행 락(2026-07-29, 라이브 전에 코드리뷰로 발견)**: `_dirty_snapshot()`의
  before/after 델타 자체가 "실행 도중 다른 프로세스가 같은 파일을 건드리는 경우까지는 완전히
  못 잡는다"고 이미 인정하고 있었는데, 그 "다른 프로세스"가 이 봇 자신일 수 있다는 걸 처음엔
  안 막았음 — 같은 별칭으로 `!코덱스`를 연달아 보내면 같은 저장소에 코덱스가
  `workspace-write`로 두 번 동시에 돌 수 있었다. `CODEX_DISPATCH_LOCKS`(별칭별
  `asyncio.Lock`)로 고침 — 이미 잠긴 별칭이면 대기 없이 즉시 거부, 다른 별칭끼리는 서로
  상태를 안 공유하므로 그대로 병렬 허용(자유 채팅의 `FREE_CHAT_LOCK`과 같은 클래스 버그,
  같은 "대기 대신 거부" 해법).
- **타임아웃**: 30분(`CODEX_DISPATCH_TIMEOUT_SECONDS`) — `codex exec`엔 자체 타임아웃
  플래그가 없어(`--help`에 없음 확인됨) 호출자 쪽(`asyncio.wait_for`)에서 건다.
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
  중인 서브프로세스 핸들)를 `kill()`. 코루틴/태스크를 취소하는 게 아니라 OS 프로세스만 죽이는
  방식 — 그러면 `handle_free_chat()`의 `await proc.communicate()`가 자연스럽게 반환되고,
  이미 있던 `proc.returncode != 0` 분기가 알아서 실패로 보고한다. 실행 중인 게 없으면 그냥
  안내만.
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

## 알려진 제약

- launchd로 상시 구동되는 프로세스라 PATH가 `/usr/bin:/bin:/usr/sbin:/sbin` 기본값으로 축소돼 있음 — `discord-bot.py`가 `weekly-report.sh`를 서브프로세스로 띄울 때 `SUBPROCESS_ENV`로 `/opt/homebrew/bin`, `~/.local/bin`을 명시적으로 앞에 붙여서 넘김. 이 PATH 문제는 이 레포 전체에서 반복 발생한 것(tmux/coach/claude/ffmpeg/whisper-cli/codex와 동일 원인) — 새 서브프로세스 스폰 지점을 추가할 때마다 재확인할 것.
- CAPTCHA·비밀번호 확인 등 Discord 개발자 포털의 본인 인증 단계는 브라우저 자동화로 우회 불가 — 봇 앱 생성/토큰 재발급은 항상 사람이 직접.
