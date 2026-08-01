# 엣지 에이전트 논리 세션 계약

상태: v1 계약 구현, 런타임 연결 전

## 목적

터미널·Telegram·Discord가 같은 작업의 진행 상태를 이어받을 수 있도록
`logical_session_id`를 공유한다. Claude·Codex·Antigravity의 native 세션을
서로 합치지는 않는다.

## 핵심 원칙

- 논리 세션과 provider native 세션은 별개다.
- `native_sessions`에는 provider별 CLI session/thread ID만 매핑한다.
- native resume은 provider adapter가 작업공간·권한·소유권을 확인한 뒤에만 수행한다.
- 전체 대화 원문과 비밀값은 세션 계약에 저장하지 않는다.
- 세션 계약은 상태 교환 형식이며, 실행·락·승인 기능을 직접 수행하지 않는다.

## 필수 데이터

| 필드 | 의미 |
|---|---|
| `logical_session_id` | 채널을 넘어 유지되는 논리 세션 ID |
| `task_id` | 실제 작업 단위 ID |
| `channel` | terminal, telegram, discord, internal |
| `provider` | 현재 처리 provider |
| `native_sessions` | provider별 native session/thread ID |
| `workspace`, `worktree`, `base_commit` | 실행 위치와 기준 commit |
| `owner`, `lease_owner`, `lease_expires_at` | 현재 실행 소유권 |
| `summary`, `decisions` | 제한된 인계 컨텍스트 |
| `changed_files`, `verification` | 작업 결과와 검증 증거 |

## 현재 적용 범위

이번 단계에서는 계약 모델과 token-free 검증만 추가했다. Telegram·Discord·터미널
런처, launchd, watchdog, Team OS 실행 경로에는 아직 연결하지 않는다.

추가 적용 범위: Telegram adapter는 `ContextEnvelopeStore`를 통해 chat별
`logical_session_id`와 제한된 entity anchor를 envelope에 기록한다. 이 연결은
provider native session을 logical session에 병합하지 않으며, 위에 적은 Telegram·
Discord·터미널 런처 전체 연결이 완료되었다는 뜻은 아니다.
