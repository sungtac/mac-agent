# 터미널 provider 표준 진입점

직접 `codex`, `agy`, `claude`를 호출하는 대신 다음 래퍼를 사용하면
`skills/edge-agent-behavior/SKILL.md`가 자동으로 프롬프트 앞에 주입된다.

```bash
bin/edge-agent-provider.sh codex /path/to/prompt.txt /path/to/worktree
bin/edge-agent-provider.sh agy /path/to/prompt.txt /path/to/worktree
bin/edge-agent-provider.sh claude /path/to/prompt.txt /path/to/worktree
```

provider 실행은 엣지 provider sandbox를 거치며, Codex는 지정한 worktree에서만
`workspace-write`로 실행된다. 이 래퍼는 권한을 확대하지 않고 공통 행동규칙과
기존 실행 경계를 하나의 표준 진입점으로 묶는다.

실행 전에는 `bin/edge_agent_capability_preflight.py`의 read-only 관측도 프롬프트에
주입된다. 관측은 실행 가능성의 근거일 뿐 권한 승인이 아니며, 관측 실패는
`unknown`으로 처리된다.
