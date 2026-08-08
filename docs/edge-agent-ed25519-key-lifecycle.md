# Ed25519 agent identity 운영 절차

이 문서는 `bin/edge_agent_ed25519_identity.py`의 opt-in identity를 운영에
도입할 때의 발급·회전·폐기 기준이다. 현재 메시지 bus의 기본값은 HMAC
compatibility이며, 이 문서의 절차를 승인하기 전에는 live key를 만들거나
기본 검증 방식을 바꾸지 않는다.

## 발급

- agent별 private key 저장소는 private directory(0700)로 둔다.
- private key는 0600, public key도 저장소 밖으로 복사할 때까지 0600으로 둔다.
- `agent_id`와 `key_id`는 안전한 식별자이며, 한 번 사용한 `key_id`는 재사용하지
  않는다.
- `generate()`는 기존 private/public 파일을 덮어쓰지 않는다. 실패 시 부분 생성물도
  제거한다.
- private key bytes는 명령행 인자·로그·message payload·이벤트 원장에 넣지 않는다.
- verifier에는 public key와 `agent_id`, `key_id`만 out-of-band로 배포한다.

## 회전

1. 새 `key_id`로 새 key pair를 발급한다.
2. verifier에 새 public key를 등록하고 fingerprint를 사람이 확인한다.
3. 새 key로 서명하는 canary를 검증한 뒤 producer를 새 key로 전환한다.
4. 기존 public key는 진행 중인 lease·재시도·replay 보존기간 동안 검증용으로 유지한다.
5. 보존기간이 끝난 뒤 기존 key를 revoked 목록으로 옮기고 private material을
   안전하게 폐기한다.

현재 구현은 단일 `expected_key_id` 검증 경계를 제공하므로 2~4단계의 overlap은
운영 verifier 설정에서 명시적으로 관리해야 한다. 자동 회전·자동 폐기는 구현하지
않았으며, 사용자 승인 없이 live key나 기본 HMAC 경계를 변경하지 않는다.

## 중단 조건

심볼릭 링크 key, group/world-readable private key, 불일치한 key ID, public key
미등록, verifier의 승인 상태 불명확, 회전 중인 task의 미확인 replay가 있으면
서명·배포·provider 실행을 fail-closed 한다.
