# Edge Agent workspace·권한 경계 기준서

상태: v1 provider CLI 경계 적용, `provider_cli_enforced`

이 문서는 엣지 에이전트와 기존 Team OS가 같은 workspace를 공유하는 상태에서,
병렬 Codex나 런타임 통합을 시작하기 전에 지켜야 할 경계를 기록한다.

## 현재 확인된 구조

- 엣지 에이전트 소스: `/Users/edge_ai/mac-agent`
- Claude·Codex·Anti Telegram 봇 workspace: `/Users/edge_ai/.openclaw/workspace`
- Team OS: `/Users/edge_ai/.openclaw/workspace/team_os`
- Team OS 상태·증거: `/Users/edge_ai/.openclaw/workspace/state`
- Team OS Telegram 실행 코드: `/Users/edge_ai/.openclaw/workspace/sukja_telegram`
- Roda Gemma workspace: `/Users/edge_ai/mac-agent`

현재 `.openclaw/workspace`는 이름과 달리 OpenClaw runtime 자체가 아니라,
엣지 에이전트와 Team OS가 함께 참조하는 기존 작업공간이다. 따라서 이 단계에서는
경로를 이동하거나 서비스를 재시작하지 않는다.

## v1 정책

1. `team_os/`, `state/`, `sukja_telegram/`은 엣지 에이전트의 직접 수정 금지 대상이다.
2. Roda Gemma는 도구·파일·셸 권한 없이 대화 전용으로 유지한다.
3. worktree 기반 병렬 실행은 전역 상태·락·병합 계약이 확정될 때까지 활성화하지 않는다.
4. 현재 manifest는 `provider_cli_enforced`다. provider CLI와 그 하위 도구의 보호 경로 쓰기를 sandbox로 차단한다.
5. Team OS 파일을 변경해야 하는 작업은 별도 승인·별도 작업공간·사후 검증을 요구한다.
6. watchdog이 관리하는 `claude-main`은 provider sandbox와 분리된 독립 세션으로 유지한다.
   별도 승인과 영향 분석 없이 sandbox를 추가하거나 working directory를 바꾸지 않는다.
7. Team OS와 엣지 에이전트는 독립 시스템으로 유지한다. 현재는 어댑터, 공유 라우터,
   공유 승인권한, 실행 결과의 자동 위임을 연결하지 않는다.

## 경계가 아직 강제되지 않는 이유

현재 Telegram Claude/Codex/Anti와 Discord의 provider 실행은 공용 workspace에서
CLI를 실행하되 `edge-agent-provider-sandbox.sh`를 거친다. 이 sandbox는 provider
CLI와 그 하위 도구의 `team_os/`, `state/`, `sukja_telegram/` 쓰기를 차단한다.
단, Codex는 자체 `workspace-write` sandbox와 외부 `sandbox-exec`를 중첩할 수 없으므로,
Codex에 대해서는 내부 sandbox를 사용하고 legacy shared workspace 실행 자체를 거부한다.
봇 자체의 로그·상태 쓰기와 watchdog이 직접 실행하는 별도 Claude 메인 세션은 이
provider CLI 경계에 포함되지 않는다.

### watchdog 세션 결정

- 상태: 독립 유지, 변경 보류
- 이유: watchdog 세션은 사용자가 직접 유지하는 Claude 작업 흐름이며, 일반 provider
  호출과 동일한 작업 계약·중지·복구 경로를 사용하지 않는다.
- 향후 변경 조건: 별도 workspace, 명시적 허용 경로, rollback, 실제 세션 smoke test를
  먼저 설계하고 사용자 승인 후에만 적용한다.

## 경로 정책 점검 명령

```bash
python3 bin/edge-agent-write-policy.py --json \
  /Users/edge_ai/.openclaw/workspace/team_os \
  /Users/edge_ai/.openclaw/workspace/projects/example.txt
```

`team_os` 아래 경로는 `protected`로 판정되고, 보호 경로 밖의 기존 workspace
경로는 `legacy_workspace_allowed`로 판정된다. `--strict`를 붙이면 허용되지 않는
경로가 하나라도 있을 때 non-zero를 반환하지만, 파일이나 서비스를 변경하지 않는다.
실제 provider 실행은 [edge-agent-provider-sandbox.sh](../bin/edge-agent-provider-sandbox.sh)를
통해 동일한 보호 정책을 적용한다.

## 다음 구현 전제

- 보호 경로 쓰기 시도를 차단하는 비파괴 테스트
- 각 서비스의 실제 working directory와 CLI sandbox 범위 검증
- 전역 nano 원장·quota 상태·provider 상태의 writer/lock 표
- 전역 작성 주체와 상태 경로의 현재 목록은 [edge-agent-global-state-inventory.md](edge-agent-global-state-inventory.md)에 기록한다.
- 별도 edge workspace 또는 명시적 허용 경로 중 하나를 선택
- 선택 후 launchd 변경, 재시작, rollback 절차를 별도 승인

## 완료 조건

- 현재 서비스·workspace·보호 경로의 소유자가 표로 설명된다.
- 보호 경로를 건드리지 않는 안전한 smoke task가 통과한다.
- 보호 경로 쓰기 시도가 차단되거나, 최소한 실행 전 단계에서 확실히 거부된다.
- 병렬 worktree가 공유 상태를 훼손하지 않는다는 근거가 있다.
- 기존 mac-agent와 Team OS의 변경사항을 섞지 않고 rollback할 수 있다.

정본 데이터는 [config/edge-agent-boundary.json](../config/edge-agent-boundary.json)이다.

Team OS와의 상호운용은 별도 프로젝트로 취급하며, 현재 경계의 최종 결정은
[edge-agent-nanoflow-project-report.md](edge-agent-nanoflow-project-report.md)에 기록한다.
