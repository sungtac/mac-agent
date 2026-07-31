# Edge Agent Rules

모든 provider와 채널에 공통으로 적용하는 행동 규칙은
[`skills/edge-agent-behavior/SKILL.md`](skills/edge-agent-behavior/SKILL.md)에 있다.

`CLAUDE.md`는 Claude 전용 진입점이고, Telegram의 Claude·Codex·Antigravity는
runtime prompt에 위 공통 계약을 자동 주입한다. 이 파일은 권한을 확대하지 않으며,
실제 서비스 설정과 worktree 계약이 우선한다.
