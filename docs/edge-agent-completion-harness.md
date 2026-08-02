# Goal-level Completion Harness

`bin/edge_agent_completion_harness.py`는 멀티에이전트 운영체제 목표의 종결 게이트다.

## 강제 규칙

- `complete`는 모든 required domain이 fresh `passed` evidence를 가질 때만 성공한다.
- `telegram_canary`는 4개 역할과 최소 3라운드가 기록된 evidence가 없으면 실패한다.
- `security_cost`, `canonical_parity`, `regression`은 `check-command`로 실제 명령을 실행해야 한다.
- 서비스가 하나라도 중단되거나 대상 저장소에 미해결 변경이 있으면 완료할 수 없다.
- 새로운 실패는 개선 task ledger에 idempotent하게 기록되고, 해당 goal은 계속 `open` 상태다.
- 완료 시점에 unresolved improvement task가 하나라도 있으면 `complete`가 거부된다.
- `--allow`는 삭제하지 않고 보존할 것으로 명시된 정확한 Git 경로에만 사용할 수
  있다. 허용 목록 밖의 변경은 계속 완료를 차단한다.

## 사용 예

```bash
python3 bin/edge_agent_completion_harness.py init \
  --goal-id multi-agent-os \
  --objective '완성된 멀티에이전트 운영체제'

python3 bin/edge_agent_completion_harness.py check-services
python3 bin/edge_agent_completion_harness.py check-repos \
  --repo /Users/edge_ai/mac-agent \
  --repo /Users/edge_ai/tools/multi-agent-starter/engine-repo \
  --allow engine-repo-macos.zip \
  --allow engine-repo-macos-v2.zip \
  --allow engine-repo-macos-v3.zip
python3 bin/edge_agent_completion_harness.py check-command \
  --domain security_cost --cwd /Users/edge_ai/mac-agent \
  --argv python3 -m unittest discover -s tests -p 'test_edge_agent_*.py'
python3 bin/edge_agent_completion_harness.py check-command \
  --domain regression --cwd /Users/edge_ai/mac-agent \
  --argv python3 -m unittest discover -s tests -p 'test_*.py'
python3 bin/edge_agent_completion_harness.py check-canary \
  --evidence-file /path/to/telegram-3-round-canary.json
python3 bin/edge_agent_completion_harness.py status
python3 bin/edge_agent_completion_harness.py complete
```

`complete`가 실패하면 그것은 보고 종료가 아니라 다음 repair cycle의 입력이다.
하네스는 실패 domain, 원인, next action, 개선 task를 보존한다. 실제 자동 수정은
provider/agent가 수행하고, 동일 하네스를 다시 통과해야만 목표를 닫을 수 있다.
