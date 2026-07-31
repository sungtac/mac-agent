---
name: code-review
description: Review full repositories, modules, files, diffs, or pasted snippets for correctness, security, performance, robustness, and maintainability. Use when the user says 코드리뷰, 코드 리뷰, 코드 점검, 코드점검, 코드 품질 검사, 코드품질검사, 코드 품질검사, or 코드품질 검사, or explicitly asks to inspect code before approval.
---

# Code Review

## Overview

이 스킬은 부분 코드와 전체 코드 모두에 적용되는 provider-neutral 코드 리뷰 계약이다. 요청을 정규화하고 실제 대상과 기준 커밋을 고정한 뒤, 독립 리뷰와 별도 승인 검증을 거쳐 근거가 있는 결과만 보고한다.

## Invocation and scope

다음 표현은 모두 동일한 code_review 의도와 같은 규칙으로 처리한다.

- 코드리뷰
- 코드 리뷰
- 코드 점검
- 코드점검
- 코드 품질 검사
- 코드품질검사
- 코드 품질검사
- 코드품질 검사

지원 범위는 diff, files, module, repo, snippet이다. 사용자가 범위를 생략하면 현재 변경사항(diff)을 우선하고, 변경사항이 없으면 요청한 파일 또는 모듈을 묻거나 명시된 범위만 검사한다. 리뷰 대상이 없는 상태에서 전체를 추정하지 않는다.

## Review workflow

1. 요청을 code_review 의도, 범위, 초점, 기준 커밋, 대상 경로로 정규화한다. 사용자가 직접 요청한 전체/부분 리뷰도 동일한 흐름을 사용한다.
2. 실제 파일, diff, 호출 맥락, 관련 테스트와 설정을 읽고 리뷰 대상의 head_sha를 고정한다. 붙여넣은 snippet은 출처와 줄 범위를 명시한다.
3. 결정론적 검사를 먼저 실행한다. 가능한 경우 formatter, linter, type checker, test, dependency/security check 결과를 증거로 첨부한다. 실행하지 못한 검사는 통과로 간주하지 않는다.
4. 1차 리뷰어는 Codex를 기본으로 사용한다. 리뷰어는 코드를 수정하거나 승인하지 않고, 재현 가능한 증거가 있는 finding만 제출한다.
5. 승인 검증자는 Antigravity를 독립 컨텍스트에서 사용한다. 1차 리뷰 결과를 그대로 신뢰하지 말고 실제 대상과 증거를 재검증한다. 필요하면 Claude를 추가 독립 의견으로 사용하되, 의견 수가 많다는 이유로 통과시키지 않는다.
6. 검증자는 finding을 확인/기각/보류하고, head_sha가 리뷰 대상과 다르면 승인을 무효화한다. blocker, 도구 실패, 대상 불일치, 불확실한 보안 문제는 CHANGES_REQUIRED 또는 ESCALATED로 닫는다.
7. 최종 보고서는 심각도 순으로 작성하고, 사람이 읽을 수 있는 요약과 구조화된 JSON을 함께 제공한다. 자동 수정, merge, 외부 전송은 이 스킬의 기본 동작이 아니다.

## Trigger policy

자동 발동은 에이전트 작업 완료 후, PR이 리뷰 준비 상태가 된 때, 그리고 같은 head_sha에서 CI가 통과한 때로 제한한다. 편집 중인 모든 저장 시점이나 모든 push마다 전체 리뷰를 자동 실행하지 않는다. 사용자가 위 동의어 중 하나로 요청하면 언제든 수동으로 전체/부분 리뷰를 시작한다.

재리뷰가 필요한 경우는 새 commit, 리뷰 대상 파일/설정의 변경, 결정론적 검사 결과 변경, 또는 이전 finding에 영향을 주는 요구사항 변경이다. 승인 결과는 head_sha에 귀속되며 다른 SHA에 재사용하지 않는다.

## Finding rules

각 finding은 파일/줄 또는 snippet 위치, 관찰한 사실, 영향, 재현 또는 검증 근거, 수정 방향을 포함한다. "나쁠 수 있다" 같은 추측만으로 blocker를 만들지 않는다.

심각도는 blocker, high, medium, low, nit이며, 기본 우선순위는 correctness/security → data loss/privacy → reliability/concurrency → performance → maintainability이다. 정적 분석 경고, 스타일 선호, 이미 존재하는 문제는 새 변경이 원인인지 구분해서 별도 표시한다.

## Output contract

구조화된 결과 계약은 references/finding-schema.json과 references/review-contract.md를 따른다. 결과 상태는 다음 중 하나다: REVIEWED, AI_APPROVED, CHANGES_REQUIRED, ESCALATED, SUPERSEDED.

AI_APPROVED는 blocker가 없고, 필요한 검사가 통과했으며, 독립 승인자가 같은 head_sha를 검증한 경우에만 사용한다. 도구가 실행되지 않았거나 결과를 파싱할 수 없으면 승인하지 않는다.

### Resource routing

- references/review-contract.md: 상태 전이, 자동 발동, provider 역할, 승인 무효화 규칙
- references/finding-schema.json: 기계 검증 가능한 보고서 스키마
- references/review-checklist.md: 정확성·보안·성능·견고성·단순성 점검 항목
- references/event-trigger-contract.md: PR/CI 이벤트를 리뷰 시작 여부로 변환하는 규칙
- scripts/normalize-review-request.py: 사용자 표현을 canonical request로 변환
- scripts/validate-review-report.py: 보고서와 승인 조건을 fail-closed로 검증
- bin/code-review-event-bridge.js: GitHub 스타일 이벤트 JSON을 로컬 리뷰 시작 판정으로 변환
- bin/code-review-webhook-server.js: 서명 검증 후 GitHub webhook을 로컬 리뷰 시작 판정으로 변환하는 HTTP 수신기
- workflows/lib/code-review-request-queue.js: 원본 payload 없이 SHA 귀속 리뷰 요청을 멱등·원자적으로 보관하는 handoff 큐
- bin/code-review-request-worker.js: clean worktree와 정확한 head_sha를 검증한 뒤 Codex/Antigravity 리뷰와 보고서 저장을 수행하는 worker
- bin/code-review-worker-runner.js: 명시적 repository allowlist를 기준으로 pending 요청을 worker에 라우팅하는 운영 runner
- bin/code-review-ops-preflight.py: launchd·repository mapping·provider 준비 상태를 변경 없이 검사하는 preflight

리뷰 결과는 로컬 런타임 상태의 code-review 저장소에 SHA별 불변 보고서로 기록한다. 원본 diff나 인증정보를 저장하지 않고, 보고서의 근거와 상태만 보존한다.
