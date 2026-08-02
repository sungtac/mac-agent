# 엣지 에이전트 파일럿 수용 기록

작성일: 2026-08-01
상태: 안전 실행 경로 구현·검증 및 Codex/Claude 단일 read-only canary 완료

## 이번 검증

- Python 관련 테스트 107개 통과
- Nano/workflow Node 테스트 48개 통과
- `git diff --check` 통과
- 기준 커밋 `7b73d0a` 생성 및 clean 상태 확인
- 파일럿 전용 clean worktree 준비: `/tmp/edge-agent-pilot-pNnY1i/worktree`
- 병렬 worktree 읽기 전용 감사 결과: finding 0건
- 자동 병합 기본값 비활성 확인
- 세션 lease·ContextStore·결과 DTO·worktree 연결 테스트 통과

## 기존 실제 provider 증거

다음 이벤트 원장을 읽기 전용으로 확인했다.

`/private/tmp/nano-provider-pilot-final-20260730/events.jsonl`

- task: `real-provider-pilot-final-20260730`
- step: `cube-implementation-1`
- status: `passed`
- verification: `light`
- changed file: `pilot.py`
- duration: 265초
- 이벤트: 1건

이 기록은 이전 실행의 성공 증거이며, 이번 검증에서 provider를 재호출한 결과가
아니다. 따라서 실제 provider 성공 표본은 현재 1건으로 취급한다.

## 운영 전환을 아직 하지 않은 이유

- 실제 Telegram·Discord·launchd 런타임에 새 세션 계층을 연결하지 않았다.
- 실제 provider 재호출은 사용량과 세션 한도를 소모한다.
- nano 임계값 분석 조건(이벤트 20건·위험 입력 10건·사용량 입력 10건)을
  아직 충족하지 않았다.
- 병렬 실행과 자동 병합은 명시적 운영 승인 전까지 비활성 상태다.
- 현재 작업 트리에 다른 기능 통합 변경이 존재하므로, 운영 연결 전 기준 commit과
  대상 파일 범위를 다시 확정해야 한다.

## 다음 전환 조건

1. 사용자가 제한적 실제 provider 파일럿을 승인한다.
2. 단일 저위험 파일·전용 clean worktree·자동 병합 없음으로 실행한다.
3. usage preflight가 확인된 경우에만 provider를 호출한다.
4. 실행 후 diff·검증·이벤트 원장·lease 해제를 확인한다.
5. 성공 표본을 누적한 뒤에만 제한적 병렬 실행을 별도로 승인한다.

승인된 단일 canary는 다음 launcher를 사용한다. 기본 실행은 계획만 만들며,
실제 provider 호출은 `--execute --confirm-live-provider`를 모두 지정해야 한다.
clean worktree와 usage gate가 통과되지 않으면 provider를 시작하지 않는다.

```bash
python3 bin/edge_agent_provider_pilot.py \
  --provider codex \
  --prompt-file /path/to/read-only-pilot-prompt.txt \
  --workdir /path/to/clean-worktree \
  --json
```

실제 호출이 승인된 경우에만 위 명령에 `--execute --confirm-live-provider`를
추가한다. 결과에는 provider 원문 대신 종료코드·출력 해시·변경 파일만 남는다.
provider 출력은 메모리에 누적하지 않고 임시 파일에서 해시·바이트 수만 계산하며,
실행 성공 판정에는 worktree 상태 확인과 `git diff --check` 통과도 포함된다.
prompt는 128KiB, 실행 timeout은 30분으로 상한을 둔다.
usage 조회가 실패해도 알려진 저사용량 차단은 유지된다. 정말 필요한 경우에만
`--allow-unmetered-provider`를 추가해 “사용량 미확인” 상태를 명시적으로 인수한다.
이 옵션은 `SKIP`로 확인된 저사용량을 우회하지 않는다.

## 2026-08-02 안전 경로 검증

- fake provider 통합 테스트로 프로세스 실행·종료코드·변경 파일 수집을 검증했다.
- provider 원문은 결과 JSON에 없고, 비밀 문자열도 결과에 남지 않음을 검증했다.
- provider 출력은 임시 파일로 스트리밍되어 Python 메모리에 누적되지 않는다.
- 실행 후 `git diff --check`가 실패하면 파일럿 전체를 실패로 판정한다.
- [x] fake provider 기반 실행·출력 비노출·diff 검증
- [x] 사용량 창 미확인·dirty worktree에서 실제 provider 시작 차단
- [x] 사용량 조회 불능 시 명시적 override 경로와 알려진 저사용량 차단 분리

## 2026-08-02 실제 Codex canary

- 대상: `/private/tmp/edge-agent-pilot-pNnY1i/worktree`의 clean Git worktree
- 작업: 파일을 변경하지 않는 read-only 상태 확인
- 사전 조건: Codex executable available, worktree clean, usage window 미확인에 대한
  명시적 `--allow-unmetered-provider` 적용
- 결과: `returncode=0`, timeout 없음, 변경 파일 0개, `git diff --check` 통과
- provider 원문은 저장·반환하지 않았고, 출력 해시와 바이트 수만 launcher가 계산했다.
- 효율성 원장에 `codex-live-canary-20260802:readonly-worktree-inspection-1` 키로
  성공 이벤트를 기록했으며 재기록은 `duplicate`로 멱등 처리됐다.

## 2026-08-02 병렬 운영 감사

- `bin/edge_agent_parallel_audit.py --repo /Users/edge_ai/mac-agent --json` 실행
- manifest/worktree 불일치·고아·예약 만료 finding: 0건
- 실제 병렬 Provider와 자동 merge는 계속 비활성 상태로 유지했다.

## 2026-08-02 차단→개선 강제 검증

- Claude usage gate 차단은 단순 실패 응답으로 끝나지 않고
  `improve-952f10a2279e9671bebf5779` queued task로 기록됐다.
- 계정 전환 후 `coach`의 `reset_min=null` 파싱 오류와 동시 Claude 세션 발견은
  `improve-1880e3a4297e5837d6b04729` 개선 task로 기록됐다.
- 개선 원장 기록 실패는 성공으로 처리하지 않으며, provider transcript·credential은
  task evidence에 포함하지 않는다.

## 2026-08-01 사용량 게이트 상태

- Claude: 5시간창 0%, 전체 상태 red — 실제 파일럿 차단
- Codex: 주간 잔여 66%, 전체 상태 green
- Dual 파일럿: Claude 차단으로 시작하지 않음

## 2026-08-02 Claude canary 및 개선 task 종료

- `coach`가 `reset_min=null`인 정상 창을 직렬화하지 못하던 오류를
  `/Users/edge_ai/tools/usage-coach/coach.py`에서 수정했다.
- null reset 회귀 테스트와 기존 window consolidation 테스트가 통과했다.
- 수정 후 사용량 게이트는 Claude 5시간 100%·7일 91%로 `PROCEED`를 반환했다.
- 전용 clean worktree의 Claude read-only canary는 `returncode=0`, timeout 없음,
  변경 파일 0개, `git diff --check` 통과였다.
- 개선 task `improve-952f10a2279e9671bebf5779`와
  `improve-1880e3a4297e5837d6b04729`는 재검증 증거와 함께 `completed`로 종료했다.
- provider 원문은 저장하지 않았고, 출력 해시·바이트 수만 결과와 효율성 원장에 남겼다.

## 남은 작업으로 기록

- [x] Claude 사용량 회복 후 `usage-preflight-gate.sh claude` 재확인 및 단일 canary 실행
- [x] 전용 clean worktree에서 단일 저위험 실제 Codex provider 파일럿 실행
- [x] 자동 병합 없이 diff·검증 확인
- [x] 파일럿 결과를 효율성 원장에 기록하고 임계값 분석 자료로 편입
- [ ] 파일럿 통과 후에만 3개 이상 병렬 provider 실행 검토
- [ ] 병렬 결과 누적 후 nano 임계값 조정
