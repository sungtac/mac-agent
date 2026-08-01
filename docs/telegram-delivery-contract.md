# Telegram 전달 계약

Provider 실행과 Telegram 전송은 서로 다른 상태 전이로 기록한다.

1. Provider가 응답을 만들면 `handoff_ready` 세션과 `delivery_pending` outbox를
   먼저 만든다.
2. 각 Telegram chunk가 확인되면 outbox의 `sent_message_ids`에 원자적으로 기록한다.
3. 모든 chunk가 기록된 뒤에만 task/session/worktree를 `completed`/`succeeded`로
   닫는다.
4. 전송 오류가 나면 provider를 다시 호출하지 않고 `전송 재시도` 요청으로 남은
   chunk만 보낸다.

Outbox는 `~/.edge-agent/state/telegram-delivery`에 mode 0700 디렉터리와 mode
0600 파일로 저장되며, 기본 48시간 뒤 만료된다. 응답은 최대 60,000자, Telegram
메시지는 최대 8개 chunk로 제한한다. 채팅방·역할·요청 사용자 ID가 모두 일치해야
재전송 원장을 조회할 수 있다.

작업 worktree에는 `edge_agent_worktree.v1` 메타데이터를 기록한다. `active`,
`delivery_pending`, `succeeded`, `failed` 상태가 전달 결과와 함께 갱신되며,
정리 도구는 등록된 Git worktree 중 terminal 상태·clean·미참조·보존기간 경과
조건을 모두 만족하는 경우에만 후보로 보고한다. 상태 확인 실패는 dirty로
간주하여 보존한다.

점검은 읽기 전용이다.

```bash
python3 bin/edge-agent-telegram-worktree-maintenance.py --json
```

정리는 후보 JSON을 검토한 뒤에만 명시적으로 실행한다.

```bash
python3 bin/edge-agent-telegram-worktree-maintenance.py --json --apply
```
