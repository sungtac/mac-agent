# 엣지 에이전트 전역 상태·작성 주체 목록

작성일: 2026-07-31

이 문서는 프로바이더 CLI 샌드박스 바깥에 남아 있는 작성 주체와 상태 경로를
분리해 기록한다. 이 문서의 목적은 즉시 제한하거나 삭제하는 것이 아니라,
추후 별도 workspace·worktree·watchdog 정책을 결정하기 위한 영향 범위를
확정하는 것이다.

## 현재 경계

| 작성 주체 | 주요 상태 경로 | 현재 보호 상태 | 판단 |
|---|---|---|---|
| Telegram Claude provider CLI 및 하위 도구 | `~/.edge-agent-worktrees/telegram-claude` | Claude 전용 Edge Agent workspace | 현재 정책에 포함 |
| Telegram Antigravity provider CLI 및 하위 도구 | `~/.edge-agent-worktrees/telegram-antigravity` | Antigravity 전용 Edge Agent workspace | 현재 정책에 포함 |
| Telegram Codex provider CLI | `~/.edge-agent-worktrees/telegram-codex` | Codex 전용 provider workspace | 현재 정책에 포함 |
| Telegram Codex canonical engine | `/Users/edge_ai/tools/multi-agent-starter/engine-repo` 및 `~/.edge-agent/state/multiagent-engine/` | `com.multiagent.engine` 단일 poller, Edge Agent sandbox·preflight 기준 | Codex token 정본 |
| Telegram 직접 Codex adapter | `/Users/edge_ai/mac-agent/bin/telegram-agent-bot.py` 및 retired plist | LaunchAgent에서 제거, plist는 `~/.edge-agent/retired-launchagents/2026-08-02/`에 mode 0600 quarantine | canonical engine 전환 완료, shared adapter는 Claude·Antigravity가 사용 |
| Discord Claude/Codex의 provider CLI 및 하위 도구 | 위와 동일 | 2026-08-02 LaunchAgent bootout + Disabled | 퇴역·quarantine 보존, active 정책에서 제외 |
| Telegram 봇 프로세스 자체 | `~/.claude/hooks-state/`, `repo-locks/`, singleton lock | sandbox 바깥 | 운영 상태·재시작에 필요 |
| Claude Stop hook·주간보고 등 사용자 세션/launchd 보조 작업 | `~/.claude/hooks-state/*`, `verify-task-v2-history.jsonl` 등 | sandbox 바깥 | provider 경계와 별도 감사 필요 |
| watchdog이 관리하는 `claude-main` 세션 | tmux 세션 및 Claude의 직접 작업 경로 | sandbox 바깥 | 독립 유지, 별도 승인 없이는 변경하지 않음 |
| Roda Gemma | Ollama 요청·봇 로그 | Team OS 쓰기 경로 없음 | 대화 전용 유지 |

## 확인된 전역 경로와 용도

| 경로 | 작성/사용 주체 | 성격 | 병렬 실행 시 주의점 |
|---|---|---|---|
| `~/.claude/discord-bot/repo-locks/` | Telegram legacy 호환·퇴역 Discord 잔여 상태 | 저장소별 flock | active Discord writer는 없음; 보존 후 별도 정리 |
| `~/.claude/hooks-state/telegram-bridge-locks/` | Telegram 봇 | 토큰별 singleton lock | 중복 poller 방지용이므로 삭제 금지 |
| `~/.claude/hooks-state/work-log/` | work-log Stop hook·Discord 재시도 | marker, 로그, debug 파일 | 민감한 세션 메타데이터 포함 가능 |
| `~/.claude/hooks-state/verify-task-nag/` | verify Stop hook | 세션별 marker | 세션 ID와 보존기간 정책 필요 |
| `~/.claude/hooks-state/session-cost-gate-nag/` | 비용 게이트 Stop hook | 세션별 marker | 비용 게이트 판정과 혼동하지 않도록 분리 |
| `~/.claude/hooks-state/usage-routing-nag/` | 라우팅 점검 hook | 세션별 marker | 라우팅 감사용, provider 상태 원장이 아님 |
| `~/.claude/discord-bot/pending/` | 퇴역 Discord 재시도 workflow | 재시도 작업 JSON | 1개 보존 중; 삭제·이동하지 않고 수동 보존 검토 |
| `~/.claude/nano-gate-events.jsonl` | nano event store | 전역 이벤트 원장 | 현재 파일 부재 확인; 생성 시 원자 append·idempotency 필요 |
| `~/.claude/verify-task-v2-history.jsonl` | verify-task-v2 workflow | 검증 이력 | worktree별 분리 또는 append lock 필요 |
| `~/.claude/provider-usage-snapshots.jsonl` | usage snapshot helper | provider 잔여량 관측 이력 | provider 호출과 분리된 로컬 관측 원장 |

snapshot 재사용은 `bin/read-provider-usage-snapshot.py`로 수행하며, 오래된 값은
`stale`로 반환한다. stale 값은 참고용으로만 사용하고 실시간 실행 허가 근거로
사용하지 않는다.
| `~/.claude-watchdog/` 및 `claude-main` | watchdog·Claude 메인 세션 | watchdog 로그/세션 제어 | provider sandbox와 다른 권한 경계 |

## 객관적 결론

1. Team OS 보호 경로에 대한 provider CLI 쓰기 차단은 적용되었다.
2. 봇 자체 상태와 watchdog Claude 세션은 아직 같은 보호 모델에 들어 있지 않다.
3. 이것은 현재 확인된 경계의 잔여 범위이지, Team OS 승인 우회가 실제로 발생했다는 증거는 아니다.
4. watchdog 세션을 바로 sandboxing하면 정상적인 사용자 Claude 작업까지 차단할 수 있으므로, 별도 승인 전에는 변경하지 않는다.
5. 병렬 worktree 도입 전에는 최소한 `repo-locks`, nano 원장, verify 이력의 저장 단위와 lock 계약을 먼저 확정해야 한다.

## 다음 구현 후보

우선순위는 다음과 같다.

1. 이 문서의 경로를 실제 파일 존재·최근 수정 시각·작성 프로세스와 대조하는 읽기 전용 감사 명령 추가
2. 전역 원장별 원자 append/lock 계약 확정
3. watchdog Claude 세션을 제한할지 여부를 별도 결정
4. 그 후에만 worktree 병렬 실행 설계

락과 전역 원장의 최소 기준은 [edge-agent-global-state-contract.md](edge-agent-global-state-contract.md)에
기준안으로 기록했다. 이 기준안이 구현·검증되기 전까지 병렬 실행은 활성화하지 않는다.

OpenClaw runtime과 공유 workspace는 퇴역시켰다. 활성 서비스는 Edge Agent 경로만
사용하며, `.openclaw`가 다시 생성되더라도 provider sandbox가 쓰기를 차단한다.

## 읽기 전용 감사 명령

```bash
python3 bin/audit-edge-agent-global-state.py
python3 bin/audit-edge-agent-global-state.py --json
```

이 명령은 경로 존재 여부·디렉터리 여부·크기·UTC 수정 시각만 읽는다. 파일 내용,
토큰, 채팅 ID는 출력하지 않으며 어떤 파일이나 서비스도 변경하지 않는다.
