---
# Delta Review — 워크플로우 리뷰 라운드 최소화 설계 (브레인스토밍 결과)

- 상태: 브레인스토밍 완료, 구현 착수(이 커밋)
- 관련 브레인스토밍 주제: "워크플로우 로직에서 리뷰 라운드를 최소화 할 수 있는 방법에 대한 고찰" (2026-08-16 시작)
- 참여: 사용자, Claude(맥), Codex, Antigravity (복수 라운드 협의)

## 배경

현재 리뷰/검증 워크플로우가 3개 존재한다.

1. **verify-task-v2** (`bin/verify-task-orchestrator.py`): 작업 시작 전 게이트. 경량/전체 티어,
   전체 트랙은 Codex 구현 → Claude+Antigravity 독립 리뷰 → 최대 2회 재시도 → 실패 시 사용자 에스컬레이션.
2. **코드리뷰 async 파이프라인** (`code-review-webhook-server.js`+`code-review-request-worker.js`):
   PR ready-for-review / green CI 트리거. Codex 1차 리뷰 → Antigravity 독립 승인. `code-review-store.js`에 결과 저장.
3. **Claude Code Workflow 툴의 즉석 다중에이전트 판정 패턴**: 세션 내 즉흥 스크립트, 영속 상태 없음.

코드 레벨로 확인한 문제 두 가지:

- **①②는 서로의 존재를 모른다.** ①은 `code-review-store.js`를 참조하지 않고(grep 0건), ②의 worker는
  결과 저장(`recordReviewReport`)만 하고 사전 조회(`findLatestReview`)는 호출하지 않는다(grep 0건, export만 되고 미사용).
  같은 커밋이 ①에서 합격된 뒤 CI green으로 ②도 트리거되면 중복 검사가 발생할 수 있다.
- **①의 재시도 라운드마다 리뷰어가 이전 라운드와 무관한 새 지적을 꺼낼 수 있다**("골대 옮기기"). 과거
  숫자 rubric(90점 기준)도 이 문제로 폐기된 전례가 있다(거의 동일 작업에 0/64/89/92점으로 채점이 흔들림,
  `docs/verify-task-v2-design.md` 참고) — 자기참조 문제(같은 모델이 스펙도 채점표도 채점도 다 함)가 원인.

## 결정 사항

### 1. 적용 범위: ①에는 규칙(게이트)으로, ②에는 정보(연속성 추적)로, ③에는 미적용

Codex·Antigravity 합의: ①은 AI가 스스로 도는 자동 재시도 루프라 무한반복 방지 목적의 강한 제약이 맞지만,
②는 사람이 자유롭게 커밋을 올리는 PR 흐름이라 같은 제약을 걸면 새로 생긴 진짜 문제를 놓칠 위험이 있다.
③은 저장소 자체가 없는 세션 내 즉흥 도구라 이 설계와 구조적으로 안 맞아 대상에서 제외.

### 2. 데이터 스키마 (①②공용 — `code-review-store.js` 확장)

새 저장소를 만들지 않고 기존 리포트 구조를 확장한다.

- 리포트: `round: N`, `parent_report_key`(직전 라운드 리포트 참조), ②는 추가로 `pr_number` 보관
- `finding.status`: `open → fixed | regression | deferred | superseded`
- `finding.location`: "파일:라인" 대신 **파일 + 앵커 스니펫 + 심볼명**(`git diff -M -C`로 이동 추적) —
  라인 번호만으로는 코드가 이동/분리될 때 오탐이 남
- ①도 `recordReviewReport()`로 기록해 ②와 저장소를 공유, `findLatestReview()`로 상호 조회 가능하게 함
  → ①②가 서로 몰랐던 문제(위 배경 절)를 데이터 레이어 통합으로 같이 해소

### 3. ① verify-task-v2 — Delta Review 게이트 (강한 제약)

- **round_origin은 리뷰어 자기보고 금지, host가 계산.** Host harness가 1라운드 open finding과 이번
  diff를 앵커 매칭으로 대조해 `matched_open_finding_id`를 미리 계산, 리뷰어는 "해소됐나/남았나"만 정성 판단.
- **2라운드 이후 허용 범위**: 프롬프트엔 1라운드 open finding의 id·앵커·원 증거·원 제목만 전달. 목록에
  없는 새 이슈는 Critical이 아니면 언급 금지 — `deferred_findings[]`로만 기록.
- **Critical 예외 3종**(그 외 반려 사유 불가): 보안 취약점 / 데이터 파괴·비가역 마이그레이션 / 기존
  테스트 회귀. Host가 결정론적 증거(테스트 실패 로그, migration diff, 알려진 취약 패턴)로 2차 검증 —
  증거 없으면 자동 `deferred` 강등.
- **deferred_findings 수명주기**: 다음 정식 전체 리뷰에 자동 상속(host가 주입) → 2회 연속 정식 리뷰에서
  안 걸리면 `stale`(주입 목록에서 제외) → 별도 라운드에서 재현되면 `open`으로 승격(정식 blocking).

### 4. ② 코드리뷰 async 파이프라인 — Delta Tracking (참고 정보, 제약 아님)

- **라운드 정의**: 같은 `repository`+`pull_request`에 새 `head_sha`가 CI green으로 검토될 때마다
  `round += 1`, 직전 완료 리포트를 `parent_report_key`로 연결(요청 생성 시점에 명시적으로 저장 —
  사후에 SUPERSEDED 체인 길이로 역산하지 않음).
- **기계적 1차 라벨링**: 이전 라운드 finding의 앵커를 이번 diff와 대조해 `fixed`/`open` 자동 판정.
- **리뷰어 프롬프트**: 이전 라운드 상태를 "참고자료"로 전달할 뿐, 신규 이슈 반려를 억제하지 않음 —
  ②는 통상적인 PR 리뷰 기준대로 새 이슈를 그대로 평가한다.

### 5. 성공 지표 / 전환 조건

- **기준선(선행 필수)**: 2026-08-17 재확인 결과, `.claude/.verify/runs/` 자체가 2026-08-15에 생성되어
  게이트 도입 후 이틀치 로그(11건, `final-verdict.json` 있는 10건 기준)밖에 없다 — "도입 전 4주"라는
  전제 자체가 이 시스템에는 성립하지 않는다(게이트가 곧 이 저장소의 검증 이력의 시작점). 참고용으로
  Delta Review 커밋(8baa692, 2026-08-17 00:58) 이전 10건의 평균 라운드 수는 2.1회(1~4회 분포,
  `final-verdict.json` 없는 1건 제외)였다. 실질적인 전/후 비교는 이 기능이 상당 기간(수 주) 운영된
  뒤에나 가능 — 재측정 시점을 정하지 말고, 운영 로그가 충분히 쌓이면(예: 총 실행 50건 이상) 다시 집계할 것.
- **롤백 조건**: `deferred_findings` 중 나중에 `open`으로 승격된 비율 > 30% → Critical 카테고리가
  너무 좁다는 증거로 보고 재조정. 도입 후 평균 라운드 수가 기준선 대비 안 줄면 기능 롤백.

## 인지하고 받아들이는 트레이드오프

- Host 판정 로직(앵커 매칭 알고리즘, Critical 증거 검증 규칙)의 정확한 세부 구현은 이 문서가 정한
  범위(무엇을 판정해야 하는지) 안에서 구현 단계가 확정한다.
- 기준선 실측 없이는 "라운드가 줄었다"를 주장할 수 없다 — 도입과 별개로 실측을 먼저 시작해야 함.
- ②의 Delta Tracking은 새 버그를 놓칠 위험을 감수하지 않는 대신, ①만큼의 라운드 감소 효과는
  기대하기 어렵다(의도적 트레이드오프 — 사람 중심 PR 흐름 보호가 우선).

## 구현 상태

- 이 문서와 함께 아래 "2부"에 정의된 최소 구현이 같은 커밋에 포함된다.
- 남은 것: 실측 기반 튜닝(기준선 측정, Critical 카테고리 재조정 여부 판단)은 운영 데이터가 쌓인 뒤 별도 작업.

## 2부. 최소 구현

목표는 "1부 설계를 실제로 동작하게 만드는 최소 변경"이다. 새 기능을 과설계하지 말고, 아래 범위로 한정한다.

### 2-1. `workflows/lib/code-review-store.js`

- 리포트 스키마에 optional 필드 추가: `round`(정수, 기본 1), `parent_report_key`(string|null, 기본 null),
  `pr_number`(정수|null, 기본 null, ②에서만 사용).
- `finding` 객체의 허용 `status` 값 검증 추가: `open | fixed | regression | deferred | superseded`.
- `finding.location`은 문자열 자유형식 유지(스키마 강제는 최소화 — 앵커 문자열을 그 안에 그대로 담는 정도).
- `findLatestReview`는 review_id 기준 조회를 유지하되, PR 기준 최신 리포트 조회 함수를 추가한다.
- 기존 `validateReport`/`recordReviewReport`/`findLatestReview` 동작과 하위 호환을 유지한다.

### 2-2. `bin/verify-task-orchestrator.py`

- 전체 트랙 재시도 루프에서 2라운드 이후 직전 라운드의 open finding 목록(id/앵커/원 증거/원 제목)만 추려 프롬프트에 포함한다.
- 리뷰 결과를 `recordReviewReport()`로 기록해 같은 저장소에 남긴다.
- 보안, 데이터 파괴·비가역 마이그레이션, 기존 테스트 회귀만 결정론적 휴리스틱과 증거가 모두 있을 때 blocking으로 허용하고, 나머지는 deferred로 강등한다.
- 기존 왕복 상한(최대 2회) 및 기존 티어 판정 로직을 보존한다.

### 2-3. `bin/code-review-request-worker.js`

- 같은 PR의 직전 완료 리포트가 있으면 `parent_report_key`와 `round`를 계산해 새 리포트에 채운다.
- 직전 리포트의 open finding과 이번 diff를 대조해 `fixed`/`open` 1차 라벨링을 참고자료로 리뷰어 프롬프트에 포함한다.
- 신규 이슈에 대한 억제/차단 로직은 추가하지 않는다.

### 범위 밖 (하지 말 것)

- 기준선 실측 자동화, 롤백 트리거 자동화는 이번 구현에 포함하지 않는다. (PR 코멘트 자동 게시 UI는
  2026-08-17 커밋 0146e8e로 별도 구현 완료 — `formatDeltaSummaryComment()` + sticky 댓글 게시,
  `gh pr comment --edit-last --create-if-none` 사용.)
- 기존 REQUIRED_FINDING_FIELDS나 상태기계(REQUESTED→...→SUPERSEDED)의 의미 자체를 바꾸지 않는다.

### 완료 기준

- `code-review-store.js`의 기존 테스트와 새 필드·상태·PR 조회 단위 테스트가 통과한다.
- `verify-task-orchestrator.py` 및 `code-review-request-worker.js` 관련 기존 테스트가 통과한다.
- 위 설계 문서 파일이 지정 경로에 존재한다.
---
