# Edge Agent 스킬 카탈로그

정본은 [skills/catalog.json](../skills/catalog.json)이다. 이 문서는 사람이 읽기
위한 요약이며, `skills/` 안의 스킬만 Edge Agent capability discovery 대상이다.

| 스킬 | 분류 | 상태 | 주요 역할 |
|---|---|---|---|
| `edge-agent-behavior` | core | active | 공통 안전·작업 행동 계약 |
| `calendar` | domain | active | Google Calendar 조회·변경 |
| `product_research` | domain | active | 공개자료 기반 제품 조사 |
| `roda-public-search` | domain | active | Roda용 제한적 공개 검색 |
| `code-review` | quality | active | 독립 코드 리뷰·승인 계약 |
| `command-registry` | safety | active | 명령 실행 전 검증·기억 |
| `harness-memory` | operations | active | 반복 오류·검증 이력 재사용 |
| `hermes_runtime` | operations | active | Hermes 증거·수명주기 점검 |
| `quota_resume` | operations | active | quota·재개·fallback 사전 검토 |

## 별도 관리 대상

- `~/.claude/skills/**`: Claude provider 전용 스킬. 이 저장소 카탈로그에 자동
  편입하지 않는다.
- `document-writing-project/hwpx-skill`, `pptx-skill`: 외부 저장소 의존성. 명시된
  alias 설정([external-skill-repositories.json](../config/external-skill-repositories.json))을
  통해서만 Codex Discord 명령에서 접근한다.
- `openclaw-backups/**`: 백업·보존 자료. 활성 스킬로 로드하지 않으며 삭제하지 않는다.

새 저장소 스킬을 추가할 때는 `SKILL.md`, 직접 테스트, 카탈로그 항목을 함께 추가하고
`python3 bin/run-skill-tests.py`와 `tests.test_skill_catalog`를 통과시켜야 한다.
`run-skill-tests.py`는 카탈로그의 `tests` 항목을 정본으로 사용하므로 별도 실행 목록을
수동으로 갱신하지 않는다.
