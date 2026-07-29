# token-cost-report.sh (토큰비용 대시보드 일일 크론)

`cron/token-cost-report.sh` — 매일 23:00 `launchd`
(`~/Library/LaunchAgents/com.macagent.token-cost-report.plist`, 레이블
`com.macagent.token-cost-report`, `Hour: 23, Minute: 0`, `Weekday` 키 없음=매일)로 실행.
Drive 설치형 포터블 스킬 `token-cost-dashboard`의 `analyze_sessions.py`를 저장소별로 돌려서
Drive `토큰비용리포트/YYYY-MM-DD/<repo>.html`에 저장한다.

## 배경 — 왜 24시간인가

실밸개발자 Claude Code 강의 2편 리뷰 후 만든 두 스킬(`ai-readiness-cartography`,
`token-cost-dashboard`) 중 이것만 크론 연동했다. 사용자가 "24시간이면 어떨까"라고
물어봐서(2026-07-29) 판단 근거를 정리했다:
- `analyze_sessions.py`는 기존 Claude Code 세션 로그를 파싱만 하는 순수 스크립트라 실행
  비용이 싸다 — `claude -p` 호출이 없어서 `weekly-report.sh`/`kakao-morning-briefing.sh`를
  괴롭히는 간헐적 헤드리스 행(hang) 이슈도 원천적으로 해당 없음.
- 실측(2026-07-29)해보니 mac-agent 하루 세션 수가 여러 건이라 일일 추세를 보는 게 말이 됨.
- 반면 `ai-readiness-cartography`는 매번 Claude가 저장소를 실제로 읽고 판단해야 해서
  실행 자체가 비싸고, 코드베이스 구조는 하루 만에 잘 안 바뀌어서 24시간 주기는 과함 —
  그래서 **크론 연동 안 함**(수동 실행 유지). 나중에 필요해지면 별도 판단.

## 대상 저장소

`discord-bot.py`의 `CODEX_REPO_ALIASES`(`!코덱스` 명령어)와 동일한 3개: `mac-agent`,
`hwpx-skill`, `pptx-skill`. 저장소 추가 필요하면 스크립트 상단 `REPOS` 배열에 한 줄 추가
(사용자 확인 후).

## exit code 구분 (2026-07-29, 이 크론 만들면서 `analyze_sessions.py`에 추가한 것)

`analyze_sessions.py`가 원래는 "세션 없음"과 "진짜 크래시"를 둘 다 그냥 실패로 뭉뚱그렸다 —
크론이 여러 저장소를 매일 도는데, 아직 그 저장소로 Claude Code 세션을 한 번도 안 켠 날이면
매번 "실패" 알림이 뜨는 스팸이 될 뻔했다. 그래서 exit code를 나눴다:
- `0` — 성공, 리포트 생성됨.
- `2` — 세션 로그 없음(그 저장소를 그날/그때까지 안 썼다는 뜻, 정상 상태) — 조용히 스킵,
  디스코드 알림 안 보냄.
- 그 외(1, 또는 처리 안 된 예외로 인한 트레이스백) — 진짜 실패, 디스코드로 알림.

## 알려진 제약

- 리포트가 매일 쌓인다(`토큰비용리포트/YYYY-MM-DD/`) — 오래되면 Drive 용량을 차지하니
  필요하면 나중에 보존 기간 정책을 따로 정할 것(아직 자동 정리 없음).
- `discord-notify.sh`는 실패 시 일방향 알림만 — Phase 2 v1 스타일 답장 재시도는 이 크론엔
  아직 없음(가치 대비 우선순위 낮다고 판단, 필요해지면 추가).
