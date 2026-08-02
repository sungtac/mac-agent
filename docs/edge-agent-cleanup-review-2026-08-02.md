# Edge Agent 통합·정리 감사 보고서

작성일: 2026-08-02
범위: `/Users/edge_ai/mac-agent`와 현재 사용자 LaunchAgent/worktree 상태
방식: read-only 구조 감사, 참조 대조, 문법·계약 테스트 실행

## 결론

현재 문제는 단순한 dead code 증가보다 실행 주체와 계약 구현이 여러 갈래로
분기된 데 있다. 삭제부터 진행하면 안 된다. 먼저 Telegram Codex와 Discord의
실제 운영 주체를 하나씩 확정하고, provider 실행·skill 선택·세션 계약의
canonical 경계를 만든 뒤 삭제 후보를 처리해야 한다.

## 기준선

- `main` HEAD: `ba8e590`, `origin/main`과 동일
- 현재 worktree: 변경·삭제·신규 파일이 함께 존재하며 추적 파일 기준 69개 변경
- 등록 worktree: 72개
- dirty worktree: 53개
- missing worktree: 0개
- 실행 코드·스킬·훅·워크플로우의 Python/JS/Shell 파일: 약 127개
- 현재 `git diff --check`: 통과

현재 변경사항은 사용자 작업으로 간주하고 보존했다. 파일 삭제, reset, 서비스
재시작, LaunchAgent 변경은 수행하지 않았다.

## 확정된 주요 이슈

## F-001 운영 차단: Codex Telegram token 소비자 중복

`python3 bin/edge_agent_auth_boundary.py --json` 결과가 다음을 보고했다.

- canonical `com.macagent.telegram-codex`: disk plist는 존재하지만 loaded 아님
- `com.multiagent.engine`: loaded 상태이며 동일한
  `~/.edge-agent/secrets/telegram/codex.token`을 참조
- audit 결과: `ready: false`
- 차단 사유: `launchd_duplicate_token_consumer:com.multiagent.engine.plist`

Telegram long polling은 token당 단일 소비자여야 한다. 어느 서비스를 정본으로
남길지는 운영 결정이므로 자동으로 하나를 끄지 않는다.

우선순위: P0

처리 결과: 최신 구현인 외부 `multi-agent-starter/engine-repo`를 Codex Telegram의
운영 정본으로 확정했다. 현재 `com.multiagent.engine`만 token을 소비하며,
`com.macagent.telegram-codex`는 LaunchAgent 파일을 보존한 채 `Disabled=true`인
호환·롤백 대기 상태다. 엔진 plist와 Edge Agent의 auth/boundary/audit 기준을
엔진으로 맞춘 뒤, `ready: true`, 중복 소비자 0개, boundary findings 0개를
재확인했다. 직접 adapter의 기능 패리티가 확인되기 전에는 해당 소스나 plist를
삭제하지 않는다.

## F-002 correctness: 표준 provider 진입점이 preflight와 skill context를 잃음

`bin/edge-agent-provider.sh:28-37`은 다음 순서로 동작한다.

1. capability preflight와 skill context를 `PROMPT`에 넣는다.
2. session context를 읽은 뒤 `PROMPT`를 새 값으로 덮어쓴다.
3. 결과적으로 앞서 만든 preflight·skill context가 provider에 전달되지 않는다.

또한 `PROMPT="$SESSION_CONTEXT\\n\\n..."` 형태는 Bash에서 실제 개행이 아니라
문자열 `\\n`을 만든다.

문서 `docs/edge-agent-provider-entrypoint.md`는 공통 행동 계약과 preflight가
자동 주입된다고 설명하므로 현재 구현과 불일치한다.

우선순위: P1

처리 결과: `bin/edge-agent-provider.sh`가 preflight·skill context·session context와
원문 요청을 모두 보존하도록 prompt 조합을 수정했고, Bash 실제 개행을 사용하도록
고쳤다. fake provider smoke와 registry 테스트로 주입 순서를 확인했다.

## F-003 구조 문제: provider 실행 진입점이 하나가 아님

표준 문서상 진입점은 `bin/edge-agent-provider.sh`지만 실제 adapter들은 다음처럼
각자 sandbox와 prompt 조합을 수행한다.

- `bin/telegram-agent-bot.py`
- `bin/discord-bot.py`
- `bin/codex-bot.py`
- `workflows/lib/score-dispatch.sh`
- `workflows/lib/codex-execute-dispatch.sh`

이 구조에서는 provider 실행 옵션, preflight 주입, session 기록, review mode가
경로별로 달라질 수 있다. sandbox 자체를 하나로 두는 것만으로는 실행 정책의
중복이 해소되지 않는다.

우선순위: P1

처리 결과: 공통 shell 진입점의 prompt 조합 결함과 dispatch 경로의 capability
resolver 사용은 정리했지만, Telegram adapter의 native session·JSONL 처리·timeout·
workspace lock까지 전면 이관하지는 않았다. 이 부분은 active adapter를 멈추지
않고 별도 runtime abstraction 설계 후 이관해야 하는 후속 작업으로 남긴다.

## F-004 구조 문제: skill 선택 정책이 세 곳으로 나뉨

현재 skill 관련 책임이 다음으로 분산되어 있다.

- `bin/edge_agent_capability_registry.py`: catalog와 trigger 기반 resolver
- `bin/edge_agent_skill_connector.py`: 위 resolver의 얇은 wrapper이지만 선택 함수가 중복
- `bin/edge_agent_skill_policy.py`: 별도의 synthetic skill trigger와 문서 주입 정책

catalog에는 없는 `context_budget`, `minimality_review`, `quota_routing`,
`verification`이 별도 정책으로 존재한다. 기능 자체가 잘못됐다는 뜻은 아니지만,
“portable skill manifest”와 “실행 정책”의 경계가 코드상 명확하지 않아 새 skill
추가·퇴역 시 한쪽만 바뀔 위험이 있다.

우선순위: P1

처리 결과: capability trigger와 synthetic policy trigger의 정본을
`edge_agent_capability_registry.py`로 모으고, connector/policy는 호환 wrapper로
유지했다. portable skill catalog와 bounded execution policy의 경계는 문서화된
상태로 보존한다.

## F-005 운영 범위 불일치: Discord는 문서·실행 상태·테스트 범위가 달랐음

`docs/improvement-plan-2026-08-01.md`는 Discord 기능·설정·데몬을 사용 중단
대상으로 명시한다. 감사 당시 launchd 상태에서는 다음이 loaded였다.

- `com.macagent.discord-bot`
- `com.macagent.codex-bot`

반대로 `requirements.txt`와 `bin/run-active-tests.sh`는 Discord를 제외하고 있었다.
즉 감사 시점의 문서·의존성·테스트·실행 상태가 서로 다른 제품 범위를 가리켰다.

우선순위: P1

처리 결과: 2026-08-02 두 LaunchAgent를 bootout했고 plist에 `Disabled=true`를
추가했다. active boundary와 테스트 runner에서 Discord를 제외하고,
`discord-notify.sh`는 외부 전송을 하지 않는 호환성 shim으로 바꿨다. pending JSON
1개와 소스는 삭제하지 않고 보존한다.

## F-006 정리 위험: 계약 전용 모듈을 dead code로 오인할 수 있음

다음 모듈은 직접적인 운영 adapter 연결보다 계약·테스트 중심이다.

- `bin/edge_agent_session_execution.py`
- `bin/edge_agent_session_lease.py`
- `bin/edge_agent_parallel_*.py`
- `bin/edge_agent_adapter_contract.py`

관련 문서도 Telegram·Discord·launchd에 아직 연결하지 않았다고 명시한다. 따라서
현재는 삭제 대상이 아니라 `contract-only / promotion pending`으로 분류해야 한다.

우선순위: P2

## F-007 저장소 위생: 생성 산출물이 ignore되지 않음

`.verify/runs/SKILL-CLEANUP-20260801/`가 untracked 상태로 남아 있고 약 96KB다.
`.pytest_cache`도 ignore되지 않는다. `.verify`는 검증 증거로 보존할지, 실행별
artifact 저장소로만 둘지 결정한 뒤 `.gitignore` 또는 명시적인 artifact 정책을
정해야 한다.

우선순위: P2

처리 결과: `.pytest_cache/`와 `.verify/runs/`를 로컬 산출물로 분류해
`.gitignore`에 추가했다. 기존 `.verify` 자료는 보존했다. worktree는 72개 중
53개가 dirty이고 missing은 0개이므로 prune·삭제하지 않고 별도 정리 기록으로
남겼다.

## 분류 제안

## Keep

- `bin/verify-task-orchestrator.py`와 `bin/verify-task-harness.py`
- `bin/edge_agent_capability_preflight.py`
- `bin/edge_agent_capability_registry.py`를 향후 skill 정본 후보로 유지
- `bin/edge_agent_auth_boundary.py`와 token-free 인증 경계 테스트
- Telegram provider adapter와 Roda Gemma adapter는 서로 다른 권한 모델이므로 당장 합치지 않음
- peer context와 Codex Telegram token owner는 외부 multi-agent starter가 관리하는
  `com.multiagent.engine`으로 확정했다. Edge Agent 쪽은 auth/boundary/audit와
  provider sandbox 계약을 제공하고, 엔진이 단일 polling·dispatch owner가 된다.

## Merge

- provider 실행·sandbox·preflight·session 기록을 공통 실행 모듈로 통합
- `edge_agent_skill_connector.py`와 `edge_agent_skill_policy.py`를 catalog 기반
  resolver + 별도 실행 정책 구조로 재편
- 반복되는 Telegram/Discord provider 호출부의 timeout, subprocess 환경,
  결과 envelope 처리를 공통화

## Deprecate / quarantine

- session/parallel 계약 모듈: 운영 연결 전까지 contract-only로 표시
- `com.macagent.telegram-codex` LaunchAgent와 Codex 전용 호환 경로: 엔진의
  delivery retry, native session, worktree/lock, plan gate, provider verification
  live canary가 끝날 때까지 비활성 호환·롤백 후보로 보존. 공유
  `telegram-agent-bot.py` 전체는 Claude·Antigravity가 사용하므로 삭제하지 않음
- Discord 관련 코드: 운영자가 퇴역을 확정하기 전까지 삭제하지 말고 loaded 상태와
  재시도 큐를 포함해 quarantine 후보로 관리
- `.verify` 실행 결과: 커밋 대상인지 로컬 artifact인지 결정 전까지 보존

## Delete candidate

현재 dirty worktree에서 이미 삭제 표시된 다음 항목은 active reference 검색상
직접 참조가 발견되지 않았다. 다만 삭제 확정은 기준선 checkpoint와 전체 테스트
후에 별도 커밋으로 진행한다.

- `workflows/verify-task-v2.js`
- `bin/run-nano-provider-pilot.sh`
- `tests/verify-task-v2-source.test.js`
- `tests/nano-gate-source.test.js`
- `tests/nano-provider-runner.test.js`
- provider별 identity markdown 3개

## 권장 실행 순서

1. `com.multiagent.engine`을 Codex Telegram token의 단일 owner로 유지하고, 직접
   adapter는 disabled 상태로 고정
2. 현재 dirty 변경을 기능별로 분리하고, 각 파일의 owner와 기준 commit을 기록
3. F-002를 수정하고 session context + preflight + skill context가 모두 provider에
   도달하는 회귀 테스트 추가
4. provider 실행 공통 경계를 만든 뒤 Telegram, Discord, code-review workflow가
   같은 결과 envelope과 정책을 사용하는지 확인
5. skill resolver를 catalog 정본과 실행 정책으로 분리하고 중복 trigger 제거
6. contract-only 모듈에 promotion gate 또는 명시적 quarantine 표식 추가
7. `.verify`와 cache artifact의 보존·ignore 정책을 결정
8. 모든 active 테스트, Node 테스트, 문법 검사, auth boundary audit을 다시 실행
9. 마지막에만 삭제 후보를 별도 커밋으로 제거

## 검증 결과

- `bash bin/run-active-tests.sh`: 통과
- `node --test tests/*.test.js`: 84개 통과
- Python AST parse: 143개 파일, 오류 0개
- Shell syntax check: 통과
- LaunchAgent plist parse: 21개, 오류 0개
- `git diff --check`: 통과
- capability preflight: provider CLI와 GitHub CLI는 available로 관측됨. 이는 권한 승인을 의미하지 않음
- auth boundary audit: 통과. `ready: true`, 중복 token consumer 0개
- boundary audit: 통과. `findings: []`, Codex service는 `com.multiagent.engine`
- engine-repo tests: 40개 통과. direct adapter의 남은 live canary와 퇴역 조건은
  `docs/engine-canonical-migration-2026-08-02.md`에 기록

## 보류 및 롤백

엔진 canonical 전환은 운영자 요청에 따라 반영하고 `com.multiagent.engine`을
재시작해 running 상태를 확인했다. 직접 Codex plist는 disabled 상태다. 후속
수정 중 문제가 생기면 엔진 plist를 기존 snapshot으로 복구하고 engine service를
bootout한 뒤, 직접 adapter의 disabled override를 제거하는 순서로 롤백한다.
token 값과 credential 내용은 롤백·검증 출력에 포함하지 않는다. dirty/active
worktree는 강제 제거하거나 `git reset`하지 않는다.
