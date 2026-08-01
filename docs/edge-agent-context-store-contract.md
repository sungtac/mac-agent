# 엣지 에이전트 컨텍스트·인계 저장소 계약

상태: v1 구현, 결정적 최신 세션 조회 적용 (2026-08-02)

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
- 최신 세션 판정은 snapshot의 `updated_at`과 논리 세션 ID의 안정적인
  tie-break를 사용한다. 파일명 순서와 filesystem mtime은 판정에 사용하지 않는다.
- 조회 명령은 `python3 ~/mac-agent/bin/edge_agent_session_bridge.py latest`이며,
  `--filter-provider`, `--filter-channel`, `--filter-workspace`로 범위를 고정할 수 있다.

작업 상태(`~/.edge-agent/state/`)는 `history.jsonl`에도 모든 상태 쓰기를 append한다.
각 이벤트에는 UTC `updated_at`, epoch, monotonic `sequence`, 요청 앞부분·끝부분,
응답 끝부분이 포함된다. `python3 ~/mac-agent/bin/edge_agent_state.py latest`가
이 원장을 기준으로 최신 작업을 고른다. `latest.json`은 호환성을 위한 포인터일 뿐이다.

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

Telegram Claude 브리지는 role별 네이티브 세션 ID를
`~/.edge-agent/state/telegram-native-sessions/<role>.json`에 mode 0600으로
atomic 저장한다. 첫 성공 요청은 `--session-id`로 세션을 만들고, 이후 요청은
`--resume`으로 같은 Claude 세션을 이어간다. 현재 logical session에도 동일한
native ID를 `native_sessions.claude`로 기록한다.

## 현재 적용 범위

ContextStore와 token-free 테스트를 유지하면서 Telegram Claude handler에
네이티브 세션 연결을 적용했다. 기존 provider별 세션 분리는 유지하며,
세션 파일이 손상되거나 없으면 새 세션으로 fail-safe 시작한다.

추가 적용 범위: Telegram의 context envelope 저장소는 기존 세션 snapshot 저장소와
분리된 `~/.edge-agent/state/telegram-context` 아래에서 chat·channel별 envelope와
entity anchor를 저장한다. Telegram adapter가 chat ID를 제공하면 native session
metadata도 chat·provider·workspace identity를 함께 확인하고, 불일치 시 resume하지
않는다. 이는 기존 logical session과 provider native session 분리 원칙을 바꾸지 않는다.
