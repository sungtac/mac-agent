# Edge Agent Rules

모든 provider와 채널에 공통으로 적용하는 행동 규칙은
[`skills/edge-agent-behavior/SKILL.md`](skills/edge-agent-behavior/SKILL.md)에 있다.

`CLAUDE.md`는 Claude 전용 진입점이고, Telegram의 Claude·Codex·Antigravity는
runtime prompt에 위 공통 계약을 자동 주입한다. 이 파일은 권한을 확대하지 않으며,
실제 서비스 설정과 worktree 계약이 우선한다.

작업 가능 여부를 판단할 때는 먼저 실제 환경을 read-only로 점검한다. 관련 도구,
비밀값을 출력하지 않는 인증 상태, endpoint·터널·서비스, 저장소 remote와
worktree 상태를 확인하고, 점검 실패(`unknown`)를 기능 부재(`unavailable`)로
단정하지 않는다. capability 확인은 권한 부여가 아니므로 외부 전송, 계정 변경,
서비스 재시작, 파괴적 변경, 유료 실행은 기존 승인 규칙을 따른다.

Telegram agent를 재시작할 때는 `bin/edge-agent-telegram-restart.py`를 사용한다.
이 helper는 진행 중인 요청을 drain하고 planned-restart marker를 남긴 뒤
재시작하므로, Roda health monitor가 정상적인 재시작을 장애로 오탐하지 않는다.
