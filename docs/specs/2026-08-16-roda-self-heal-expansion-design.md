# 로다 인시던트 자가치유 시스템 설계 (v3, 자동수정 확장)

- 상태: 브레인스토밍 + 코덱스·안티그래비티 2회 다자간 검토 완료, 구현 계획은 미착수
- 관련: 기존 `docs/specs/2026-08-16-roda-role-escalation-design.md`(Task 1~7, 이미 구현·커밋됨) 위에 얹는 확장
- 검증 이력: 1차 상의(코덱스 56/100, 안티그래비티 50/100) → 5가지 반영 v2 → 2차 상의(코덱스 88/100, 안티그래비티 91/100) → 4가지 추가 반영 v3(이 문서)

## 배경

기존 완료 시스템(Task 1~7)은 "누가 챙길지"를 자동 배정하는 에스컬레이션 체인이지, 실제로
코드를 고치는 자가치유는 아니었음. 별도로 존재하던 코덱스 단독 자동수정
(`_run_codex_repair_impl`)은 fingerprint별 사람 승인이 필요해 "완전 자동"이 아니었고,
코덱스 하나에만 의존해 코덱스가 다운되면 아무도 못 고침.

사용자 요청: 승인 게이트를 없애 완전 자동 트리거로 가되, 독립검토는 유지하는 "자가치유
시스템"으로 확장. 코덱스와 안티그래비티에게 직접 의견을 구해 즉석 채점표로 평가받고,
지적사항을 반영해 적합도·완성도 90% 이상 확보.

## 결정 사항

### 1. 전체 흐름 (사용자 승인: 방식 A — 자동치유 먼저, 실패해야 에스컬레이션)

```
사고 감지
   |
   v
[자동치유 시도] (escalation_stage = "auto_repairing", 기존 ack타이머 일시정지)
   전체 단계 하드타임아웃 5분. 순서대로 시도(동시 아님):
   1. 코덱스 (단, 코덱스 자신이 usage_limited/rate_limited 등 동적장애면 건너뜀)
   2. 코덱스 실패/불가 -> 클로드
   3. 클로드도 실패/불가 -> 안티그래비티
   4. 5분 하드타임아웃 도달 또는 셋 다 실패 -> "자동치유 실패" 확정
   |
   +-- 성공 -> 종료, 담당자 알림 없음, incident resolved
   |
   +-- 실패 -> escalation_stage="awaiting_ack", routed_at=지금으로 타이머 리셋,
              기존 Task 1~7 체인(담당자 재확인 ->5분ack->24시간완료->안티판단->회의) 시작
```

`NON_REPAIRABLE_CODES`는 자동치유 시도 자체를 건너뛰고 바로 위 "실패" 분기로 직행(기존
로직 유지). `main_dirty`는 특히 "고칠 대상"이 아니라 저장소 잠금 상태이므로 자동치유
후보에서 아예 제외.

### 2. 구현자 실행 계약 (구조화된 반환값)

`_run_implementer_cli(role, prompt) -> dict`로 통일, 반환 스키마:

```
{
  "status": "success" | "no_change" | "apply_failed" | "timeout" | "provider_error",
  "diff": str | None,          # 적용된 unified diff (감사용)
  "changed_files": list[str],
  "exit_code": int | None,
  "timed_out": bool,
  "stderr_tail": str,          # 마지막 500자만, 토큰/시크릿 절대 포함 금지
}
```

role별 구현:
- codex: 기존 `codex exec -s workspace-write --skip-git-repo-check -C <worktree> --json`
  결과를 위 스키마로 매핑 (이미 구현된 `_run_codex_repair_impl`의 파싱 로직 재사용)
- antigravity: `agy --print --mode accept-edits` (worktree 안에서, `--mode plan` 금지)
  실행 후 `git status --porcelain`으로 changed_files 계산
- claude: `claude -p --output-format json`으로 순수 unified diff 텍스트를 요청하는
  프롬프트를 보냄. 받은 텍스트에서 마크다운 코드펜스(```diff ... ```)를 벗겨내고
  `git apply --check`로 먼저 검증 → 통과해야 실제 `git apply` 실행. `git apply` 실패나
  `--check` 실패 시 "apply_failed"로 즉시 다음 구현자에게 폴백, worktree는 폐기(재사용 안 함).

각 구현자 호출 자체는 여전히 180초 타임아웃(기존 코덱스 자동수정과 동일 관례) 유지하되,
**전체 자동치유 단계는 5분 하드타임아웃**을 별도로 걸어 셋을 순서대로 다 돌아도 5분을
넘기면 즉시 중단하고 에스컬레이션 체인으로 넘어감(3×180초=9분이 되는 걸 방지).

### 3. 자동치유 성공의 단일 정의

아래를 전부 만족해야 "성공"으로 간주 (하나라도 실패하면 "실패"로 처리하고 다음 구현자
시도 또는 에스컬레이션 체인으로):
1. 구현자 호출이 `status: success`
2. 변경된 파일이 보호 파일 목록(§4)에 하나도 없음
3. 저장소 전체 자동 테스트 스위트 100% 통과 (2명 검토 트랙이든 1명 예외 트랙이든 예외 없음)
4. 독립 검토 통과 (§5 기준)
5. main 병합 + 서비스 재기동 성공
6. 재기동 후 같은 fingerprint의 재처리가 확인됨(기존 recovery_watch 메커니즘 재사용)

### 4. 보호 파일 목록 (항상 2명 검토 고정, 1명 예외 불가)

인증/시크릿 관련 파일, launchd plist, 상태 스키마 마이그레이션 코드
(`_migrate_state`/`STATE_SCHEMA_VERSION` 관련 라인), `verify-task-orchestrator.py` 자신,
그리고 **의존성·빌드·배포 파일**: `requirements.txt`, `package.json`, `Dockerfile`,
`.github/workflows/*`, `go.mod`, `Gemfile`, `pyproject.toml` 등 공급망에 영향을 주는
파일 전부.

### 5. 병합 조건

기본 정책: 자동수정을 안 한 나머지 2명 모두 검토 통과 + 전체 테스트 100% 통과.
1명만 통과해도 병합 허용하는 예외는 **아래 3가지를 전부 만족할 때만**:
- diff가 보호 파일(§4)을 하나도 안 건드림
- 변경 줄 수 합계 30줄 이하, 변경 파일 수 3개 이하
- 전체 테스트 스위트 100% 통과 (2명 트랙과 동일 기준, 예외 없음)

검토 가능한 로봇이 0명(나머지 둘 다 다운/사용량제한)이면 자동 병합 절대 금지, 에스컬레이션
체인으로.

### 6. 3중 폭주 방지 장치 (원자적 카운터)

기존 상태 파일(`telegram-health-monitor.json`)의 원자적 쓰기(`_atomic_write` + 기존
`integration_lock`) 관례를 그대로 재사용해 아래 카운터를 원자적으로 증가:
- fingerprint당 24시간 내 자동치유 시도 최대 2회. 초과 시 `auto_repair_blocked` 표시,
  즉시 에스컬레이션 체인 전환, 재시도 없음
- 저장소 전체 24시간 내 자동병합 최대 3건. 초과 시 전체 자동치유가 "수동 승인 모드"로
  자동 전환(이 상태에선 기존 fingerprint별 사람 승인 게이트가 부활). 사람이 명시적으로
  해제해야 정상 모드 복귀 — 자동 만료 없음
- 여러 인시던트가 동시에 자동치유를 시작해도 카운터 증가는 `integration_lock` 보호 하에
  일어나므로 레이스 컨디션으로 상한을 넘는 일이 없음

### 7. 재발 대응 (병합 후 신뢰도 하락)

병합 후 1시간 이내 같은 (role, code)가 재발하면:
1. 해당 병합 커밋 `git revert` 시도
2. revert가 충돌 없이 성공 → 그 fingerprint를 `auto_repair_blacklist`에 영구 등록
   (이후 그 fingerprint는 자동치유 영구 건너뛰고 바로 에스컬레이션 체인)
3. **revert가 충돌하면**: 자동으로 충돌 해결을 시도하지 않고 즉시 `git revert --abort`,
   "되돌리기 실패(충돌)"를 최우선 등급 알림으로 사람에게 직접 전송(에스컬레이션 체인의
   일반 배정 절차를 건너뛰고 최상위 긴급 알림)

### 8. NON_REPAIRABLE_CODES 재분류

- 상시 제외(자동치유 시도 자체를 안 함, 바로 에스컬레이션): auth_error, context_exceeded,
  main_dirty(저장소 잠금 상태라 "고칠 코드"가 없음)
- 동적 제외(그 장애를 일으킨 구현자만 순번에서 제외, 다른 구현자는 정상 시도):
  usage_limited, rate_limited, capacity_limited, service_overloaded — 건강모니터의
  기존 `usage_watch` 상태를 순번 결정에 참조

### 9. 감사·상태 복구

각 자동치유 시도마다 아래를 incident 레코드에 영구 기록(재시작 후에도 유지):
구현자별 호출 결과(§2 스키마), 선택된 diff 요약, 검토 결과, 테스트 로그 경로, 병합 커밋
해시, revert 여부/사유, 블랙리스트 등재 사유. 상태 전이(`auto_repairing` →
`awaiting_ack`/`resolved`/`auto_repair_blocked`)는 idempotent해야 하며, 프로세스가
재시작되거나 폴링 주기가 겹쳐도 같은 작업이 두 번 실행되거나 같은 커밋이 중복 병합되지
않아야 함(기존 `pending_merges` 큐잉 관례 재사용 가능).

## 다자간 검증 이력

- 1차: 코덱스 56/100, 안티그래비티 50/100 (자동치유 CLI 부재, 폭주방지 없음, 1인검토
  위험, 핸드오프 타이밍 미정, NON_REPAIRABLE 재분류 필요 — 공통 지적)
- 2차(위 §1~8 초기 버전 반영): 코덱스 88/100, 안티그래비티 91/100
- 3차 반영(이 문서, §2·3·4·5·6·7·9 보강): 코덱스 예상 92~94/100 (실제 3차 재채점은
  생략, 사용자 승인 하에 문서화 진행)

## 다음 단계 (미착수)

`writing-plans` 스킬로 구현 계획 작성. 이 확장은 기존 `roda-telegram-health-monitor.py`에
대규모로 추가되는 작업이라 여러 Task로 쪼개야 함 — 최소 다음 경계가 예상됨: (1) 구현자
실행 계약 통일 + 클로드 래퍼 신설, (2) 자동치유 성공정의 + 병합조건(보호파일/저위험예외),
(3) 3중 폭주방지 카운터, (4) 재발대응(revert+블랙리스트), (5) NON_REPAIRABLE 동적제외,
(6) 에스컬레이션 체인과의 핸드오프(auto_repairing 상태, 5분 하드타임아웃), (7) 감사기록.
실제 코드 변경은 verify-task 게이트(코덱스 구현 + 클로드/안티 독립검토) 필수.
