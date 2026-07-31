# Health-repair worktree 보존 정책

- 기본 보존기간: 30일
- 최대 보존 용량: 2GiB
- 활성 Git worktree는 정리 대상에서 제외
- 기본 실행은 읽기 전용 inventory/dry-run
- 실제 정리는 명시적으로 `--apply`를 지정한 경우에만 수행
- 정리 대상은 `~/.edge-agent-worktrees/health-repairs`의 직접 하위 디렉터리로 제한
- 실패 원인·커밋·로그를 확인할 수 있도록 보존기간 전에는 삭제하지 않음

점검:

```bash
python3 bin/edge-agent-health-maintenance.py --json
```

정리 실행은 후보 목록을 먼저 검토한 뒤 별도로 수행한다.

```bash
python3 bin/edge-agent-health-maintenance.py --json --apply
```
