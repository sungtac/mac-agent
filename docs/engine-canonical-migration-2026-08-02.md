# Codex Telegram engine 정본 전환 기록

작성일: 2026-08-02

## 결정

최신 구현인 `/Users/edge_ai/tools/multi-agent-starter/engine-repo`를 Codex
Telegram의 정본으로 사용한다. 운영 token 소비자는 `com.multiagent.engine` 하나로
제한한다. 기존 `/Users/edge_ai/mac-agent/bin/telegram-agent-bot.py`는 Claude·
Antigravity와 공유되는 adapter이므로 파일 전체를 삭제할 수 없다. Codex 전용
LaunchAgent만 `Disabled=true`인 호환·롤백 경로로 보존하고, Codex 전용 분기는
공유 adapter에서 별도 분리한 뒤에만 제거를 검토한다.

이 결정은 물리적으로 두 저장소를 즉시 합치는 것과는 다르다. 엔진 저장소는
오케스트레이션·Telegram transport의 정본이고, mac-agent는 Edge Agent의
provider sandbox, capability preflight, skill resolver, auth/boundary/audit 계약의
정본이다. 엔진은 이 계약을 호출하지만 credential 값이나 별도 token owner를
복제하지 않는다.

## 반영된 운영 경계

- `config/edge-agent-boundary.json`의 Codex service: `com.multiagent.engine`
- `com.multiagent.engine.plist`: `TELEGRAM_BOT_TOKEN_FILE`은 canonical secret,
  `MULTIAGENT_ENGINE_ROOT`는 `/Users/edge_ai/mac-agent`
- `com.macagent.telegram-codex.plist`: 디스크에 보존되지만 disabled
- auth boundary: 두 plist의 기대 경로를 알고, disabled direct plist는 active
  duplicate로 세지 않음
- boundary audit: engine의 `telegram/adapter.py`를 Codex process script로 인정
- restart/health monitor: Codex 역할을 `com.multiagent.engine`과
  `~/.edge-agent/state/multiagent-engine/stderr.log` 기준으로 감시
- engine adapter: provider sandbox를 통해 Codex를 실행하고 capability preflight,
  skill context, peer context, runtime contract를 bounded prompt에 주입

## 현재 기능 패리티

엔진에서 확인된 기능:

- Telegram Bot API single poller와 chat allowlist
- 명령형 task 상태·승인·취소·재시도·artifact 조회
- 자연어 Codex bounded turn과 read-only 기본 sandbox
- Edge Agent provider sandbox 및 capability/skill/peer/runtime context 주입
- auth boundary와 launchd 상태 감사에 연결된 단일 token owner
- durable delivery outbox: provider 재실행 없이 chunk별 전송 상태·retry·만료 보존
- bounded logical session snapshot/event journal
- clean-base task worktree와 repository lifecycle lock 계약
- durable plan approval gate와 독립 verification consensus 계약

아직 live lifecycle에서 직접 adapter와 동등하게 확인하지 못한 기능:

- 실제 Telegram transport에서의 delivery retry 및 재시작 복구
- 실제 provider native session resume와 세션별 상태 복구
- provider 실행과 task worktree/lock lifecycle의 live 연결
- 실제 Claude/Antigravity provider verification loop
- provider별 상세 결과 envelope와 장시간 작업 진행 상태 전달

따라서 직접 adapter는 아직 삭제 대상이 아니다. 엔진에 위 기능의 계약은 이관했지만,
실제 provider 작업 lifecycle에 대한 live canary와 장시간 운영 관찰은 아직 남아
있다. 각 계약의 회귀 테스트와 live canary를 모두 통과한 뒤에만 소스·plist 삭제를
검토한다.

## 직접 adapter 퇴역 조건

다음 조건을 모두 충족해야 한다.

1. engine-repo에 delivery retry, session, worktree/lock, plan gate,
   provider verification의 parity test가 있다.
2. 허용 chat, token owner, read/write sandbox, capability/skill context에 대한
   contract test가 통과한다.
3. 실제 Telegram canary에서 명령·자연어·승인·실패 재시도·장시간 작업을 확인한다.
4. 최소 관찰 기간 동안 engine 단일 poller와 delivery 원장의 중복·유실이 없다.
5. engine service를 disabled direct adapter로 되돌리는 rollback rehearsal가
   성공한다.
6. 두 저장소의 dirty 변경과 기준 commit을 별도 checkpoint로 기록하고 운영자
   승인을 받는다.

조건이 충족되기 전까지 Codex LaunchAgent plist와 Codex 전용 호환 테스트는
quarantine 상태로 보존한다. 공유 `telegram-agent-bot.py`와 Claude·Antigravity
경로는 계속 active로 유지한다. 삭제나 worktree prune은 이 기록의 범위에
포함하지 않는다.

## 오프라인 canary 결과

2026-08-02 로컬 temporary state에서 실제 Telegram API와 provider를 호출하지 않고
다음 흐름을 통과시켰다.

`engine accept → durable plan approval → logical session → delivery outbox →
chunk delivery → independent Claude/Antigravity verification`

delivery status는 `succeeded`, verification consensus는 `true`였다. 이 결과는
외부 Telegram 전송 성공을 의미하지 않으며, 실제 canary는 별도 승인이 필요하다.

## 퇴역 승인 gate

read-only 판정 명령:

```bash
python3 bin/edge_agent_engine_retirement.py --json
```

현재 gate는 rollback 대상 보존과 shared adapter 소유권을 확인했고, 다음 세 가지를
승인 대상으로 명시한다.

- 실제 Telegram canary 1회 전송
- Codex 전용 LaunchAgent plist의 recoverable quarantine 이동
- Claude·Antigravity 경로를 건드리지 않는 Codex 전용 분기 분리·제거

gate 자체는 서비스·파일·credential을 변경하지 않는다. 실제 canary와 quarantine는
운영자 승인 후 별도 단계로 실행한다.

현재 gate 실행 결과의 temporary-copy rollback rehearsal는 `passed: true`다.
엔진 plist와 disabled direct plist의 복사본을 quarantine→restore하고 byte equality를
확인했으며, 실제 LaunchAgent·파일에는 변경이 없었다.

## 검증 기준선

- `python3 -m unittest discover -s /Users/edge_ai/tools/multi-agent-starter/engine-repo/tests`: 40개 통과
- `python3 -m unittest tests/test_edge_agent_auth_boundary.py tests/test_edge_agent_boundary_audit.py`: 8개 통과
- auth boundary: `ready: true`, duplicate 0개
- boundary audit: `findings: []`
- `com.multiagent.engine`: running
- `com.macagent.telegram-codex`: disabled / loaded 아님

credential 값 자체는 어느 출력에도 포함하지 않는다.
