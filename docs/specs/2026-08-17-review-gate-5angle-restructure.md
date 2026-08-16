# Review gate 5-angle restructure

## 배경

`run_full()`의 독립 리뷰는 Claude 1회와 Antigravity 1회에 의존했다. 한 관점의 누락이나 단일 provider의 형식 오류가 전체 판단을 흔들 수 있고, 이전 라운드의 Delta Review finding을 다음 라운드에서 일관되게 재검증하기 어려웠다.

## 최종 설계

- Antigravity는 `rules-compliance`, `shallow-bugs`, `git-history-scope`, `historical-regression`, `doc-comment-sync`의 5개 고정 각도를 매 라운드 병렬 실행한다. 기존 완성도 체크리스트를 공통으로 사용하고 각 issue에 confidence를 요구한다.
- Antigravity issue는 confidence 80 미만이면 Python 단계에서 제외한다. 개별 dispatch 실패나 잘못된 JSON은 해당 각도만 건너뛰며, 성공 각도가 0개일 때만 기존 blocking 진단을 추가한다. 실패 각도와 원인은 라운드 history에 남긴다.
- `historical-regression`은 전역 `code-review-store`의 현재 저장소 대상 최근 5개 report를 읽기 전용으로 조회한다. report 요약은 각각 2000자 이내로 자르고, store가 없거나 읽기에 실패하면 `과거 리포트 없음`으로 처리한다. `record_delta_report()`가 사용하는 per-run store는 변경하지 않는다.
- Claude는 Antigravity와 병렬로 1차 탐색하고, 두 결과의 후보를 중복 제거한 뒤 2차 순차 재검증한다. 후보는 file+symbol+description 기준으로 합치고 `origin_sources`를 보존하며, confidence 내림차순 상위 20개와 evidence 1000자 제한을 적용한다. 최종 blocking 판정은 2차 결과만 사용한다.
- `normalize_issue()`가 Antigravity와 Claude 결과를 기존 Delta Review issue schema로 통일한다. 따라서 `match_open_finding`, `is_critical_delta_issue`, `delta_suffix`, `record_delta_report`, `previous_open/deferred` 흐름의 계약은 유지된다.
- provider 출력에 잘못된 UTF-8 바이트가 포함되어도 `Popen(text=True, errors="replace")`로 오케스트레이터가 종료되지 않게 한다.

## 판단 근거

Codex의 구현·검증 관점에서는 기존 Delta Review 매칭 키와 기록 경로를 보존하는 것이 가장 중요했다. Antigravity의 다섯 관점은 규칙 준수, 얕은 correctness/security/robustness, git 범위와 의도, 과거 회귀, 문서·주석 계약이라는 서로 다른 실패 원인을 분리한다. Claude 2차 재검증은 후보를 실제 diff와 대조해 독립 리뷰의 오탐을 최종 승격 전에 줄인다. 부분 실패를 메타데이터로 남기면서 성공 결과를 계속 사용하는 것은 한 provider의 일시적 실패가 정상 리뷰를 막지 않도록 하기 위한 판단이다.

## 제외 범위

나노단위 스트리밍 리뷰와 light 트랙(`light_review_prompt`, `run_light`)은 이번 설계 대상이 아니다. 기존 Delta Review 함수의 시그니처·동작과 per-run 기록 경로도 변경하지 않는다.
