# 논리 세션 실행 Lease 계약

상태: v1 구현, provider 런타임 연결 전

## 목적

Telegram과 터미널이 같은 `logical_session_id`를 동시에 재개하지 못하게 한다.
작업공간 lock과 별도로 세션 소유권을 보호한다.

## 동작

- 세션별 lock 파일에 POSIX `flock(LOCK_EX | LOCK_NB)`를 사용한다.
- 획득 성공 시 lease metadata를 atomic rename으로 기록한다.
- lease 해제 시 metadata 상태를 `released`로 남긴다.
- 프로세스가 비정상 종료해도 OS가 flock을 해제하므로 오래된 metadata 자체는
  새 실행을 막는 권위 있는 상태로 사용하지 않는다.
- 하나의 세션 안에서는 provider를 바꿔도 동시에 두 provider를 실행하지 않는다.
- 서로 다른 logical session은 별도 lease로 병렬 실행할 수 있다.

## 제한

현재는 lease primitive과 token-free 테스트만 추가했다. 실제 Telegram·터미널
handler가 lease를 획득하도록 연결하지 않았으며, 연결 전까지 기존 실행 경로는
변경되지 않는다.
