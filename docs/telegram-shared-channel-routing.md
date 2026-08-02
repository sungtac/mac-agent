# Telegram shared-channel routing

상태: 2026-08-02부터 일반 무주소 자연어 fan-out 적용 및 canary 검증 완료

## 현재 동작

허용된 그룹에서 사람이 보낸 일반 자연어는 Claude, Codex engine,
Antigravity, Roda가 각각 처리한다. `코덱스야`, `안티야`, `로다야`, `@bot` 같은
명시 주소도 계속 지원하며, 다른 역할을 직접 지칭한 요청은 기존 역할 분리 규칙을
따른다. 봇이 보낸 메시지는 무한 재응답 방지를 위해 무시한다.
Roda는 다른 봇을 대상으로 한 slash command에도 응답하지 않는다. slash command는
무주소 자연어 fan-out 대상이 아니며, 해당 봇만 처리한다.

provider 봇과 Codex engine은 각각 자신의 Telegram token으로 한 번씩 polling한다.
따라서 token 중복 consumer가 생기지 않도록 `edge_agent_auth_boundary.py`를
변경 후 확인한다.

## Roda privacy mode 제약

Roda 코드 자체는 무주소 그룹 메시지를 처리하도록 바뀌었지만, Telegram Bot API의
privacy mode가 켜져 있으면 Telegram이 봇에 무주소 그룹 메시지를 전달하지 않는다.
privacy mode가 켜진 경우에는 `can_read_all_group_messages=False`로 나타나며,
무주소 메시지가 Roda에 전달되지 않는다. 현재 운영 상태는 Roda가 그룹
administrator이고 `can_read_all_group_messages=True`로 확인됐다.

4개 봇 fan-out을 실제로 활성화하려면 Telegram에서 Roda 봇의 privacy mode를
해제하거나 해당 봇을 그룹 administrator로 승격해야 한다. 이 설정은 로컬 코드나
LaunchAgent가 변경할 수 없으므로 운영자가 Telegram UI/BotFather에서 수행해야 한다.
설정 후 `can_read_all_group_messages=True` 또는 administrator 상태를 로그에서
확인한다. 2026-08-02 05:28 KST `canary`에서 Claude·Codex·Antigravity·Roda
모두 처리 완료했고, 네 응답 모두 원본 메시지에 reply로 연결됐다.

2026-08-02 post-restart canonical Codex delivery canary도 성공했다:
`canonical-canary-20260802-r2`, Telegram `message_id=285`, durable delivery
status `succeeded`. 이는 canonical outbound/API 경로의 실측이며, 사용자 발화에
대한 네 모델 fan-out 자체의 새 live canary와는 구분한다.

## 검증 명령

```bash
python3 tests/test_antigravity_identity.py
python3 /Users/edge_ai/tools/multi-agent-starter/engine-repo/tests/test_telegram.py
python3 bin/edge_agent_auth_boundary.py --json
python3 bin/audit-edge-agent-boundary.py --json
```

Codex engine은 원본 Telegram `message_id`를 durable delivery record에 보존하고
`reply_parameters`로 전송한다. 따라서 chunk 분할이나 재시도에서도 원본 메시지
reply 관계가 유지된다. 다른 봇 대상 slash command는 Roda가 처리하지 않는다.
