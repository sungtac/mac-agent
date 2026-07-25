# discord-bot.py + discord-notify.sh (일정비서 — Discord 연동)

Phase 1 (사용자 요청, 2026-07-26): 온디맨드 트리거 + 일방향(Mac→Discord) 실패/에스컬레이션 알림. Phase 2(에스컬레이션 양방향 응답), Phase 3(자유 채팅)는 아직 설계만 있고 미구현.

## 구성

- `bin/discord-bot.py` — 상시 구동 프로세스(Discord Gateway WebSocket 연결). `~/Library/LaunchAgents/com.macagent.discord-bot.plist`로 launchd 상시 등록(`KeepAlive: true`, `RunAtLoad: true`) — 주간보고서처럼 주기 실행이 아니라 항상 떠 있어야 함. `~/.claude/discord-bot/venv`(격리된 venv, `discord.py 2.7.1`)로 실행.
- `bin/discord-notify.sh <message>` — 봇 프로세스와 무관하게 Discord REST API로 메시지 한 번 보내는 헬퍼. 실패해도 항상 exit 0 — 알림 실패가 호출한 스크립트(주간보고서 등)를 절대 죽이면 안 됨.
- 설정: `~/.claude/discord-bot/config.json` (`{"token":..., "channel_id":...}`) — 이 레포 밖, `chmod 600`. 토큰은 Discord 개발자 포털(https://discord.com/developers/applications)에서 발급, "Message Content Intent"를 반드시 켜야 봇이 메시지 내용을 읽음.

## 권한 경계 (사용자가 명시적으로 결정한 것, 재검토 없이 그냥 넓히지 말 것)

- **인가된 채널만**: `config.json`의 `channel_id` 하나만 반응, 그 외 채널/DM/다른 서버/봇 자신의 메시지는 전부 무시. 이게 신뢰 경계 전부 — 그 채널은 초대받은 사람 전원이 완전히 신뢰된다는 사용자의 명시적 결정.
- **자유 채팅(Claude Code에 임의 지시)은 본인만**: 아직 Phase 1이라 자유 채팅 자체가 없지만, Phase 3 구현 시에도 이 권한만은 채널 내 다른 사람에게 안 열기로 사용자가 이미 결정함 — 온디맨드 트리거/에스컬레이션 응답과 분리된 별도 권한 레벨.

## Phase 1 명령어

- `!주간보고서` — `weekly-report.sh`를 즉시 실행(최대 20분 watchdog). 완료/실패 결과를 같은 채널에 보고.
- `!상태` — 오늘자 weekly-report 로그 tail + 최근 work-log 처리 3건 요약.
- 그 외 메시지는 전부 무시(Phase 1은 명령어 패턴만, 자유 채팅 릴레이 없음).

## 에스컬레이션 알림 연결 지점 (일방향)

- `cron/weekly-report.sh` — 3회 재시도 다 실패하면 `discord-notify.sh` 호출.
- `hooks/work-log-stop-check.sh` — watchdog 타임아웃 시 `discord-notify.sh` 호출.
- `workflows/verify-task-v2.js` — `needsUserDecision`/`needsClarification`로 끝나면(경량/전체 트랙 공통) `discord-notify.sh` 호출.

## 알려진 제약

- launchd로 상시 구동되는 프로세스라 PATH가 `/usr/bin:/bin:/usr/sbin:/sbin` 기본값으로 축소돼 있음 — `discord-bot.py`가 `weekly-report.sh`를 서브프로세스로 띄울 때 `SUBPROCESS_ENV`로 `/opt/homebrew/bin`, `~/.local/bin`을 명시적으로 앞에 붙여서 넘김. 이 PATH 문제는 이 레포 전체에서 반복 발생한 것(tmux/coach/claude/ffmpeg/whisper-cli/codex와 동일 원인) — 새 서브프로세스 스폰 지점을 추가할 때마다 재확인할 것.
- CAPTCHA·비밀번호 확인 등 Discord 개발자 포털의 본인 인증 단계는 브라우저 자동화로 우회 불가 — 봇 앱 생성/토큰 재발급은 항상 사람이 직접.
