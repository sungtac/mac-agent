# 엣지 에이전트 파일럿 수용 기록

작성일: 2026-08-01
상태: 기준선 확정, 실제 provider 파일럿 대기

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

## 2026-08-01 사용량 게이트 상태

- Claude: 5시간창 0%, 전체 상태 red — 실제 파일럿 차단
- Codex: 주간 잔여 66%, 전체 상태 green
- Dual 파일럿: Claude 차단으로 시작하지 않음

## 남은 작업으로 기록

- [ ] Claude 사용량 회복 후 `usage-preflight-gate.sh dual` 재확인
- [ ] 전용 clean worktree에서 단일 저위험 실제 provider 파일럿 실행
- [ ] 자동 병합 없이 diff·검증·nano 이벤트·lease 해제 확인
- [ ] 파일럿 결과를 효율성 원장에 기록하고 임계값 분석 자료로 편입
- [ ] 파일럿 통과 후에만 3개 이상 병렬 provider 실행 검토
- [ ] 병렬 결과 누적 후 nano 임계값 조정
