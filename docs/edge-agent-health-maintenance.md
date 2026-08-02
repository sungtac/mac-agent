# Health-repair worktree 보존 정책

- 기본 보존기간: 30일
- 최대 보존 용량: 2GiB
- 활성 Git worktree는 정리 대상에서 제외
- 상태 확인에 실패했거나 dirty인 worktree는 정리 대상에서 제외
- 기본 실행은 읽기 전용 inventory/dry-run
- 실제 정리는 명시적으로 `--apply`를 지정한 경우에만 수행
- 정리 대상은 `~/.edge-agent-worktrees/health-repairs`의 직접 하위 디렉터리로 제한
- 실패 원인·커밋·로그를 확인할 수 있도록 보존기간 전에는 삭제하지 않음

점검:

```bash
python3 bin/edge-agent-health-maintenance.py --json
```

정리 실행은 후보 목록을 먼저 검토한 뒤 별도로 수행한다. 강제 삭제는 사용하지
않으며, Git이 clean 상태를 다시 확인하지 못하면 보존한다.

```bash
python3 bin/edge-agent-health-maintenance.py --json --apply
```

Telegram task worktree는 별도 정책을 사용한다. 기본 점검은 읽기 전용이며,
활성 세션·승인 대기·dirty 변경이 있거나 terminal 상태가 아닌 worktree는
정리 후보가 되지 않는다.

```bash
python3 bin/edge-agent-telegram-worktree-maintenance.py --json
```

실제 정리는 후보 JSON을 검토한 뒤에만 명시적으로 실행한다. dirty worktree는
git worktree remove 대상에서 제외되어 수동 검토를 위해 보존된다.

```bash
python3 bin/edge-agent-telegram-worktree-maintenance.py --json --apply
```

## Telegram agent 재시작

Telegram provider를 직접 `launchctl kickstart -k`로 종료하지 않는다. 진행 중인
요청을 끊고 Roda가 일시적인 `service_down`으로 오판할 수 있다. 표준 경로는
다음 helper이며, active request drain·planned-restart marker·startup 확인을
수행한다.

```bash
python3 bin/edge-agent-telegram-restart.py antigravity --reason "maintenance"
```

Roda health monitor는 planned marker가 유효한 동안 감지를 억제하고, marker가
없는 경우에도 90초 grace period가 지난 뒤에만 `service_down` 자동복구를 시작한다.

Codex가 수정·커밋·병합·재기동하는 자동복구는 기본 비활성화다. 별도 승인 파일을
만들 때만 다음과 같은 fingerprint별 기간 한정 승인 기록이 인정된다.

```json
{
  "approvals": {
    "<event fingerprint>": {"approved": true, "expires_at": 1790000000}
  }
}
```

승인 파일 경로는 `RODA_GEMMA_AUTO_REPAIR_APPROVAL_FILE`로 지정할 수 있다.

Telegram 작업공간 생성은 repository lifecycle lock 충돌 시 기본 5회, 1초
간격으로 비동기 재시도한다. 환경 변수로 조정할 수 있다.

```text
TELEGRAM_AGENT_WORKTREE_LOCK_RETRIES
TELEGRAM_AGENT_WORKTREE_LOCK_RETRY_SECONDS
```

Provider가 outer handler의 `처리 실패` 로그 전에 종료하더라도 `exit=<code>`와
`empty response`를 완료 이벤트로 인식해 pending 요청이 불필요하게 no-response
상태로 남지 않게 한다.
