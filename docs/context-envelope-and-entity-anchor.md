# Telegram context envelope와 entity anchor

상태: v1 구현

## 목적

Telegram의 논리 대화 식별자와 짧은 후속 발화를 provider 공통 형식으로 전달한다.
이 기능은 provider native session을 합치지 않으며, 전체 대화 원문도 저장하지 않는다.

## Schema

`ContextEnvelope`는 다음 필드를 가진다.

- `schema_version`, `channel`, `provider`
- `chat_id`, `message_id`, `reply_to_message_id`
- `logical_session_id`, `created_at`, `anchor_reference`

`EntityAnchor`는 `chat_id`, `source_message_id`, `kind`, `sanitized_value`,
`confidence`, `created_at`을 저장한다. 봇 응답 message ID는 내부 binding 목록으로만
연결해 사용자의 reply를 원본 anchor에 되돌린다.

## 저장 안전성

기본 저장 루트는 `~/.edge-agent/state/telegram-context`이며, 테스트와 운영 도구는
`ContextEnvelopeStore(root)`로 루트를 주입할 수 있다. chat과 channel별 JSON 파일 및
별도 lock 파일을 사용하고, `fcntl.flock`으로 read-modify-write 전체를 보호한 뒤
임시 파일과 `os.replace`로 원자화한다. 손상 JSON·권한 오류는 `unavailable` 결과로
바꾸고 provider 실행 자체는 임의로 중단하지 않는다.

URL의 query와 fragment는 제거한다. URL·경로·주제 전체에서 token, api_key, password,
bearer, cookie, secret 등의 민감 패턴이 발견되면 값은 `[redacted]`로 치환한다.
주제는 공백을 정리하고 최대 240자로 제한한다.

## Resolver 규칙

초기 기본 TTL은 900초, retention 기간은 86400초, 기본 confidence는 0.35다.
900초와 0.35는 08:44의 영상 링크 뒤 08:47에 들어오는 짧은 후속 발화를 우선
연결하기 위한 초기 configurable default이며, 운영 데이터가 쌓이면 조정할 수 있다.

- 명시적인 사용자 메시지 또는 봇 응답에 대한 reply는 TTL보다 우선해 `resolved`다.
- 최근 유효 anchor가 하나면 지시어·후속 작업 표현을 `resolved`로 연결한다.
- 최근 유효 anchor가 둘 이상이면 `ambiguous`로 provider 실행을 막는다.
- 유효 anchor가 없고 만료 anchor만 있으면 `stale`로 명확화를 요청한다.
- 그 밖에는 `none`이며 envelope ID만 provider에 전달한다.
- 저장소를 읽을 수 없으면 `unavailable`이며 안전한 envelope만 전달한다.

인사말은 단순 길이로 후속 발화로 판정하지 않는다. `이거`, `그 영상`,
`팩트체크`, `분석`, `요약`, `확인`, `검토해줘` 같은 명시 표현과 anchor 존재를
함께 확인한다.

## Provider 연결

`telegram-agent-bot.py`는 일반 대화, 코딩 계획, Claude의 Codex 위임, Codex 검증·
재작성 루프를 포함한 모든 CLI 호출에 동일한 envelope와 sanitized anchor block을
전달한다. `ambiguous`와 `stale`에서는 provider를 호출하지 않고 waiting 상태와
명확화 안내를 남긴다. Claude native session metadata에는 chat, provider, workspace
identity가 포함되고 chat별 파생 파일을 사용한다. chat ID가 없는 기존 helper 호출은
기존 경로를 유지한다.

`roda-gemma-bot.py`도 같은 준비 절차를 사용한다. 원본 사용자 메시지만 anchor 후보로
처리하고 짧은 대명사 후속 문장은 새 anchor로 저장하지 않는다. Ollama 응답 message
ID는 binding API에 기록한다.
