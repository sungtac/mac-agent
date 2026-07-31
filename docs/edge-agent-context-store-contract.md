# 엣지 에이전트 컨텍스트·인계 저장소 계약

상태: v1 구현, 런타임 연결 전

## 저장 구조

```text
~/.edge-agent/sessions/
├── snapshots/<logical_session_id>.json
├── events/<logical_session_id>.jsonl
└── locks/<logical_session_id>.lock
```

- snapshot은 임시 파일 작성 후 atomic rename한다.
- event journal은 한 줄 JSONL로 append하고 `fsync`한다.
- snapshot·event 접근은 세션별 `flock`으로 직렬화한다.
- 손상된 JSON·지원하지 않는 schema·민감정보 표식은 성공으로 처리하지 않는다.

## 컨텍스트 정책

공유하는 것은 전체 transcript가 아니라 다음의 bounded 정보다.

- 요약
- 다음 작업
- 결정사항
- 위험 메모
- 변경 파일
- 검증 결과

provider native session/thread ID는 `native_sessions`에 provider별로 따로 저장한다.
따라서 Telegram에서 terminal로 인계해도 Claude·Codex·Antigravity의 대화 이력을
서로 직접 합치지 않는다.

## 현재 적용 범위

ContextStore와 token-free 테스트만 추가했다. 실제 Telegram·Discord handler,
터미널 launcher, Team OS adapter, launchd에는 아직 연결하지 않는다.
