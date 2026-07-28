# discord-bot.py + discord-notify.sh (일정비서 — Discord 연동)

Phase 1 (사용자 요청, 2026-07-26): 온디맨드 트리거 + 일방향(Mac→Discord) 실패/에스컬레이션 알림. Phase 2 v1(사용자 요청, 2026-07-28): `weekly-report.sh` 실패 알림에 답장하면 재시도. Phase 2.5(사용자 요청, 2026-07-28): `work-log-stop-check.sh` 실패 알림에 답장하면 재시도. verify-task-v2 헤드리스 재호출은 여전히 미구현(재개 메커니즘 자체가 없어 별도 인프라 필요), 자유 채팅(Phase 3)도 아직 미구현.

## 구성

- `bin/discord-bot.py` — 상시 구동 프로세스(Discord Gateway WebSocket 연결). `~/Library/LaunchAgents/com.macagent.discord-bot.plist`로 launchd 상시 등록(`KeepAlive: true`, `RunAtLoad: true`) — 주간보고서처럼 주기 실행이 아니라 항상 떠 있어야 함. `~/.claude/discord-bot/venv`(격리된 venv, `discord.py 2.7.1`)로 실행. 코드를 고치면 재기동 필요: `launchctl kickstart -k gui/$(id -u)/com.macagent.discord-bot`.
- `bin/discord-notify.sh <message>` — 봇 프로세스와 무관하게 Discord REST API로 메시지 한 번 보내는 헬퍼. 실패해도 항상 exit 0 — 알림 실패가 호출한 스크립트(주간보고서 등)를 절대 죽이면 안 됨. Phase 2부터 성공 시 게시된 메시지의 Discord id를 stdout으로 반환(실패 시 빈 문자열) — 호출한 스크립트가 그 id로 pending-job을 기록해 나중에 답장을 매칭할 수 있게 함.
- 설정: `~/.claude/discord-bot/config.json` (`{"token":..., "channel_id":..., "free_chat_user_id":...}`) — 이 레포 밖, `chmod 600`. 토큰은 Discord 개발자 포털(https://discord.com/developers/applications)에서 발급, "Message Content Intent"를 반드시 켜야 봇이 메시지 내용을 읽음. `free_chat_user_id`는 아직 미구현인 Phase 3(본인 전용 자유 채팅)을 위해 미리 넣어둔 값 — 현재 코드는 참조하지 않음.
- `~/.claude/discord-bot/pending/<message_id>.json` — Phase 2 pending-job 저장소(레포 밖, git 추적 안 함). 에스컬레이션을 쏜 스크립트가 `discord-notify.sh`가 반환한 message id로 기록: `{"type":"weekly-report-retry"|"work-log-retry","created_at":ISO시각,"params":{...}}` — `weekly-report-retry`는 `params`가 비어있고(자기완결 스크립트라 외부 상태 불필요), `work-log-retry`는 `params`에 `session_id`/`transcript_path`를 담는다(work-log-stop-check.sh가 Stop 훅 stdin JSON 없이는 어느 세션인지 알 방법이 없어서). `discord-bot.py`가 답장을 받으면 `message.reference.message_id`로 이 파일을 찾아 `type`에 따라 디스패치하고 처리 후 즉시 삭제(중복 답장 방지). 48시간 지난 항목은 만료 처리. `type`을 못 알아들으면(향후 미구현 소스 등) 조용히 로그만 남기고 무시 — 스키마가 깨지지 않고 확장 가능.

## 권한 경계 (사용자가 명시적으로 결정한 것, 재검토 없이 그냥 넓히지 말 것)

- **인가된 채널만**: `config.json`의 `channel_id` 하나만 반응, 그 외 채널/DM/다른 서버/봇 자신의 메시지는 전부 무시. 이게 신뢰 경계 전부 — 그 채널은 초대받은 사람 전원이 완전히 신뢰된다는 사용자의 명시적 결정.
- **자유 채팅(Claude Code에 임의 지시)은 본인만**: 아직 Phase 1이라 자유 채팅 자체가 없지만, Phase 3 구현 시에도 이 권한만은 채널 내 다른 사람에게 안 열기로 사용자가 이미 결정함 — 온디맨드 트리거/에스컬레이션 응답과 분리된 별도 권한 레벨.

## Phase 1 명령어

- `!주간보고서` — `weekly-report.sh`를 즉시 실행(최대 20분 watchdog). 완료/실패 결과를 같은 채널에 보고.
- `!상태` — 오늘자 weekly-report 로그 tail + 최근 work-log 처리 3건 요약.
- 명령어도 아니고 pending-job에 대한 답장도 아닌 메시지는 전부 무시(자유 채팅 릴레이 없음).

## Phase 2 v1 — weekly-report.sh 답장 재시도

세 에스컬레이션 소스 중 `weekly-report.sh`만 재시도가 안전하다고 판단해(2026-07-28) 이것부터
구현했다: 재실행 자체가 안전하고, 유일한 중복 위험(Calendar 이벤트 매번 새로 생성)은 4번
단계에 `search_events` 선확인 가드를 추가해 막았다. 나머지 둘은 이 시점엔 일방향으로
남겨뒀다:

- `verify-task-v2.js`의 `needsUserDecision`/`needsClarification`은 재개(resume) 메커니즘
  자체가 없다 — 유일한 복구 경로가 "질문에 답 → 전체 워크플로우를 처음부터 재호출"이고, 이걸
  헤드리스(활성 터미널 없이)로 하려면 `agent()` 셔임을 새로 만들어야 해서 여전히 미구현.
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

## 에스컬레이션 알림 연결 지점

- `cron/weekly-report.sh` — 3회 재시도 다 실패하면 `discord-notify.sh` 호출 + pending-job 기록(양방향, 답장으로 재시도 가능).
- `hooks/work-log-stop-check.sh` — 실패(타임아웃 포함) 시 `discord-notify.sh` 호출 + pending-job 기록(양방향, 답장으로 재시도 가능). 성공 시에도(LOGGED일 때만) 알림.
- `workflows/verify-task-v2.js` — `needsUserDecision`/`needsClarification`로 끝나면(경량/전체 트랙 공통) `discord-notify.sh` 호출(일방향, 재개 메커니즘 없어 재시도 미구현).

## 알려진 제약

- launchd로 상시 구동되는 프로세스라 PATH가 `/usr/bin:/bin:/usr/sbin:/sbin` 기본값으로 축소돼 있음 — `discord-bot.py`가 `weekly-report.sh`를 서브프로세스로 띄울 때 `SUBPROCESS_ENV`로 `/opt/homebrew/bin`, `~/.local/bin`을 명시적으로 앞에 붙여서 넘김. 이 PATH 문제는 이 레포 전체에서 반복 발생한 것(tmux/coach/claude/ffmpeg/whisper-cli/codex와 동일 원인) — 새 서브프로세스 스폰 지점을 추가할 때마다 재확인할 것.
- CAPTCHA·비밀번호 확인 등 Discord 개발자 포털의 본인 인증 단계는 브라우저 자동화로 우회 불가 — 봇 앱 생성/토큰 재발급은 항상 사람이 직접.
