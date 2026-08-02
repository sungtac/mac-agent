# Edge Agent 인증 저장소 이관 계획

## 목표

모든 로컬 인증 파일의 기준 위치를 `~/.edge-agent/secrets/` 아래로 통일한다.
인증 파일의 내용은 에이전트 입력, 로그, 상태 보고서에 포함하지 않는다.

## 현재 기준 위치

- Calendar OAuth: `~/.edge-agent/secrets/calendar/`
- Claude/Codex/Antigravity Telegram bot token: `~/.edge-agent/secrets/telegram/`
- Roda Telegram bot token: `~/.edge-agent/secrets/roda-gemma/`
- Code Review webhook secret: `~/.edge-agent/secrets/code-review-webhook.secret`

## 단계

1. `python3 bin/edge_agent_auth_boundary.py --strict --json`으로 현재 상태를 기록한다.
2. `python3 bin/migrate_edge_agent_auth.py`로 복사 계획을 확인한 뒤, 운영자가 소유권을 확인한 알려진 인증파일만 `--apply --confirm-copy`로 canonical 경로에 복사한다. 기존 canonical 파일은 기본적으로 덮어쓰지 않으며, 파일 내용은 출력하지 않고 권한은 `0600`으로 유지한다.
3. LaunchAgent의 `*_TOKEN_FILE` 및 `CODE_REVIEW_WEBHOOK_SECRET_FILE`을 canonical 경로로 변경한다.
4. 서비스를 재시작하지 않고 plist와 파일 권한을 먼저 재감사한다.
5. 승인된 운영 창에서 각 서비스의 연결 상태를 확인한다. 토큰 값 자체는 로그에 남기지 않는다.
6. Calendar, Roda, Telegram 에이전트의 실제 동작을 최소 canary로 검증한다.
7. 일정한 관찰 기간 후 OpenClaw의 중복 인증파일을 백업·보존 정책에 따라 별도 처리한다. 이번 실행에서는 삭제하지 않고 `~/.edge-agent/legacy-auth-quarantine/2026-08-01/`로 격리 보관했다.

현재 LaunchAgent가 구형 경로를 참조하는 상태에서는 서비스 재시작을 수행하지 않는다.
canonical 파일 복사와 LaunchAgent 경로 변경은 별도 승인된 운영 창에서 한 번에
진행하고, 인증 내용은 로그·보고서·검증 프롬프트에 포함하지 않는다.

## 중단 조건

- canonical 파일이 없거나 권한이 `0600`보다 느슨한 경우
- LaunchAgent가 legacy 경로를 계속 참조하는 경우
- `openclaw_env`의 소유·사용처·보존 정책이 확인되지 않은 경우
- 실제 인증값을 출력해야만 확인할 수 있는 상황

## 자동화 경계

`bin/edge_agent_auth_boundary.py`는 읽기 전용 감사기다. 인증파일 복사, 삭제, 서비스 재시작은 수행하지 않는다. 실제 credential copy/delete는 명시적인 운영 승인과 별도 실행 단계가 필요하다.
