# Active Edge Agent 개선 계획

이 계획은 2026-08-01 평가 결과와 Antigravity의 읽기 전용 레드팀 조사 결과를
합쳐 확정한 실행 범위다. Discord 기능·설정·데몬은 사용 중단 대상이므로 이
계획과 테스트 진입점에서 제외한다.

## 근거 자료

- [Telegram Bot API: getUpdates](https://core.telegram.org/bots/api#getupdates): 동일
  토큰의 long polling은 단일 소비자여야 하며 중복 호출은 Conflict가 된다.
- [Apple launchctl manual](https://developer.apple.com/library/archive/documentation/Darwin/Reference/ManPages/man1/launchctl.1.html):
  disk plist 변경과 loaded launchd job은 별도 상태이며 bootstrap/bootout 등으로
  명시적으로 반영해야 한다.
- [Python Packaging User Guide](https://packaging.python.org/en/latest/guides/installing-using-pip-and-virtual-environments/):
  의존성 선언과 가상환경 기반 설치로 실행 환경을 재현한다.
- [POSIX Shell Command Language](https://pubs.opengroup.org/onlinepubs/9699919799/utilities/V3_chap02.html):
  인자 배열과 인용을 보존하지 않는 wrapper는 분리·주입 위험을 만든다.
- [git-worktree documentation](https://git-scm.com/docs/git-worktree): dirty 또는
  활성 worktree는 강제 제거하지 않고 등록·상태를 확인해야 한다.

## 병렬 파이프라인

| 파이프라인 | 변경 표면 | 완료 조건 |
| --- | --- | --- |
| P1 런타임 정합성 | `bin/edge_agent_auth_boundary.py`, 관련 테스트, LaunchAgent 상태 | loaded 환경과 disk plist drift가 검출되고 canonical 상태로 reload 후 재검증 |
| P2 Telegram 단일 소비자 | `bin/telegram-agent-bot.py` 계약 테스트 | token별 singleton lock → cooldown → polling 순서가 유지되고 Conflict 회귀가 통과 |
| P3 재현성 | `requirements.txt`, `bin/run-active-tests.sh`, `.github/workflows/active-tests.yml` | active non-Discord Python/Node 테스트와 compile/diff 검사가 동일 진입점으로 실행 |
| P4 실행·작업공간 경계 | provider sandbox와 worktree inventory 테스트 | Codex 경로 옵션 변형이 동일하게 보호되고 dirty/active worktree가 보존·보고 |

## 실행 순서와 중단 조건

P1·P2·P4는 서로 다른 파일 표면에서 병렬 구현할 수 있다. P3은 테스트
진입점을 제공한다. 이후 통합 단계에서 모든 테스트를 실행하고, 마지막에
LaunchAgent를 drain-aware하게 reload한다.

다음 경우에는 자동 진행을 중단한다.

- 인증 토큰 내용 접근 또는 비밀값 복사가 필요해지는 경우
- dirty/active 작업을 삭제·force 제거해야만 진행되는 경우
- provider live API 호출이 필요해지는 경우
- Discord 파일을 기능 변경해야만 해결되는 경우
