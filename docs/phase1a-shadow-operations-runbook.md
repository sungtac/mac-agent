# Phase 1-A Shadow 운영 안전장치 Runbook v2

이 문서는 Phase 1-A3-2-0 오프라인 계약과 향후 제한적 Canary 운영 절차를 정의한다.
문서 적용만으로 Feature Flag, LaunchAgent, Runtime, Shadow root 또는 HMAC key가 활성화되거나
생성되지 않는다. 실제 운영 적용·재시작·삭제·Canary 활성화는 각각 별도 사용자 승인을 필요로 한다.

## 1. 현재 운영 범위와 기본 OFF

현재 Shadow Observer는 관찰 전용이다.

| 항목 | 현재 상태 |
| --- | --- |
| EDGE_AGENT_SHADOW_OBSERVER_ENABLED 기본값 | OFF |
| 중앙 Task Router | 비활성 |
| 실행 Claim·Lease | 실행 제어에 사용하지 않음 |
| Provider 선택·실행 차단 | 수행하지 않음 |
| Telegram 응답 생성 | 기존 Legacy Provider Bot이 담당 |
| 중앙 승인 흐름 | 비활성 |
| Shadow Telegram 출력 | 비활성 |

EDGE_AGENT_SHADOW_OBSERVER_ENABLED가 미설정되거나 0, false, no, off이면 Shadow Observer는 비활성이다.
잘못된 값이나 필수 설정 오류도 ON으로 추정하지 않고 OFF 또는 No-op으로 전환한다.

OFF 상태에서는 Shadow 모듈 초기화, queue·worker·DB·JSONL 생성, HMAC key 접근, 운영 root 생성이 없어야 한다.
Legacy Telegram 경로는 계속 동작한다. 설정 변경은 대상 Bot 재시작 후에만 적용되며, 이 Runbook 적용은
Flag를 활성화하지 않는다. Canary 활성화는 별도 사용자 승인 대상이다.

현재 활성 기능은 Telegram ingress metadata 관찰, logical event와 Bot observation 기록, Shadow 상태·용량·복구
검증이다. 다음 기능은 현재 비활성이다.

- 중앙 라우팅 및 응답 Bot 선정
- 단일 실행 소유권 강제 및 Claim 기반 중복 실행 차단
- Provider 호출 통제
- 파일 writer 선정
- 중앙 승인 흐름

Shadow DB에 Claim 관련 필드나 구조가 존재하더라도 이번 단계의 실행 제어에는 사용하지 않는다.
Telegram 응답과 Provider 실행은 기존 Legacy Bot의 책임이다.

## 2. 저장 원본·상태·회계

SQLite는 Shadow 이벤트와 현재 상태의 유일한 authoritative source다. JSONL은 SQLite transactional outbox에서
파생되는 감사 로그이며 현재 상태의 원본이 아니다.

상태는 NORMAL, SOFT_LIMIT, HARD_LIMIT, READ_ONLY_DEGRADED, RECOVERING을 사용한다.
모든 제출 이벤트는 다음 회계를 만족해야 한다.

submitted = processed + dropped_queue_full + dropped_disk_budget
          + rejected_stopping + abandoned_shutdown_timeout + failed_store

Health snapshot에는 원문, 파일명, Token, 실제 첨부 경로, Provider prompt/response, HMAC key 원문 또는
사용자 개인정보를 포함하지 않는다.

## 3. 운영 기본값 및 용량 계약

Runtime의 기본값은 다음과 같다. 문서의 MB/GB 표기는 운영 계약 표기이며, 구현은 해당 binary byte 값을 사용한다.

| 항목 | 운영 계약 표기 | Runtime 기본값 |
| --- | ---: | ---: |
| SQLite hot retention | 30일 | 30일 |
| 닫힌 JSONL retention | 14일 | 14일 |
| SQLite 최대 크기 | 512MB급 | 512 * 1024 * 1024 bytes |
| JSONL segment 최대 크기 | 256MB급 | 256 * 1024 * 1024 bytes |
| Shadow root soft limit | 768MB급 | 768 * 1024 * 1024 bytes |
| Shadow root hard limit | 1GB급 | 1024 * 1024 * 1024 bytes |
| retention batch | 500 rows | 500 |
| maintenance 주기 | 1시간 | 3600초 |
| HMAC key rotation | 90일 | 90일 |

실제 Canary 전에는 event 평균 크기와 일일 유입량을 측정해 이 값을 재승인한다.

## 4. 권한·경로 안전 계약

| 대상 | 필수 mode |
| --- | ---: |
| Shadow root directory | 0700 |
| SQLite DB | 0600 |
| JSONL segment | 0600 |
| manifest | 0600 |
| health snapshot | 0600 |
| lock file | 0600 |
| temporary file | 0600 |
| HMAC key file | 0600 |

활성화 전 다음을 검사한다.

1. Shadow root가 실제 directory인지 확인한다.
2. root 또는 key가 symlink이면 활성화를 거부한다.
3. root와 key 소유자가 현재 서비스 사용자와 일치하는지 확인한다.
4. root가 0700보다 넓거나 파일이 0600보다 넓으면 활성화를 거부한다.
5. Token 경로, 기존 운영 state·session·plan·delivery root, 운영 worktree와 같거나 그 하위인 경로이면
   활성화를 거부한다.

권한 오류 시 Shadow 활성화를 중단하고 No-op 또는 OFF로 전환하며, 민감한 실제 경로를 일반 로그에 남기지 않는다.
권한을 상위 디렉터리까지 재귀적으로 변경하지 않는다.

## 5. Maintenance 명령

다음은 검증된 maintenance subcommand 계약이다.

status
retention-dry-run
retention-execute
purge-all-dry-run
purge-all-execute
verify

모든 삭제·변경 명령의 기본은 dry-run이다. 논리적 명령 계약의 실제 CLI entrypoint와 운영 schedule은
별도 승인 없이는 추가하지 않는다. 모든 명령은 명시적 --root를 요구하고, root canonicalization·symlink·
운영 경로 보호 검사를 먼저 수행해야 한다.

## 6. SQLite retention

retention-execute 전제조건:

- 명시적 Shadow root가 지정되어야 한다.
- root가 허용된 실제 directory이고 symlink가 아니어야 한다.
- maintenance lock을 획득해야 한다.
- DB가 읽기 가능하고 후보를 계산할 수 있어야 한다.
- 삭제 후보가 사전 retention-dry-run 결과와 일치해야 한다.
- active lease, pending outbox, recovery candidate, unresolved failure, quarantine event가 후보에 없어야 한다.

삭제 대상은 terminal 상태 AND retention 기준일 초과 AND JSONL outbox 전달 완료 AND active lease 없음
AND recovery candidate 아님 AND unresolved failure 아님 AND quarantine 상태 아님을 모두 만족해야 한다.

retention-execute 절차:

Feature Flag와 Observer 상태 확인
→ maintenance lock 획득
→ 후보 재계산
→ dry-run 결과와 비교
→ bounded batch transaction
→ terminal event와 완료 outbox 삭제
→ commit
→ 삭제 row·추정 byte 기록
→ health 상태 갱신

retention-dry-run은 읽기 전용이다. retention-execute는 운영 데이터 삭제 작업이므로 초기 Canary에서는
사용자 승인 후에만 실행한다. 자동 schedule 활성화도 별도 승인 대상이다. 승인 없이 운영에서 실행하지 않으며,
정상 운영 중 SQLite full vacuum은 자동 실행하지 않는다.

ACTIVE, CLAIMED, RECOVERY_CANDIDATE, FLUSHING, PENDING_OUTBOX, FAILED_UNRESOLVED, QUARANTINED 상태는
자동 삭제하지 않는다. 실패 시 transaction rollback, Legacy Telegram 계속, 삭제 완료 기록 금지, 다음 실행 전
verify, 무제한 재시도 금지를 적용한다.

## 7. JSONL rotation·보존

현재 segment를 flush하고 fsync한 뒤 process-safe rotation.lock으로 직렬화하여 timestamp/sequence 이름으로
atomic rename한다. 새 segment와 임시 파일은 0600이다. 256MB급 segment 한도 또는 시간 경계에서 rotation한다.
닫힌 segment만 14일 retention 대상이며 active segment는 자동 삭제하지 않는다. partial line이 없어야 하며,
corrupt segment는 quarantine하고 SQLite 조회와 Legacy Telegram 처리는 계속한다.

JSONL append·rotation·retention 실패가 SQLite authoritative event를 훼손하지 않는다. 여러 Bot process는
lock 계약으로 동시에 rotation하지 않으며, JSONL은 SQLite 상태를 덮어쓰지 않는다.

## 8. HMAC key lifecycle와 key_id

HMAC은 본문 개인정보 보호 fingerprint만 담당하며 실행 identity가 아니다.

body_fingerprint:
  algorithm: HMAC-SHA256
  key_id: hmac-<non-secret-identifier>
  value: <HMAC result>

key_id는 비밀값이 아닌 식별자이며, fingerprint마다 사용한 key_id를 event payload의 body_hmac_key_id
metadata로 기록한다. key 원문은 DB·JSONL·manifest·health·로그에 저장하지 않는다. key 누락·권한 오류·
읽기 실패 시 fingerprint는 UNKNOWN이며, 임의의 단순 SHA-256 fallback을 사용하지 않는다.

프로세스 초기화 때 key 파일을 한 번 읽고 메시지마다 다시 읽지 않는다. key rotation은 process restart
또는 명시된 reload 방식으로만 적용하며, 이전 fingerprint를 새 key로 재작성하지 않는다. key 파일은 0600,
root는 0700이어야 한다. 기본 rotation 주기는 90일이다.

## 9. Task Identity와 HMAC rotation 독립성

root_task_id, revision_id, event_id 및 cross-bot logical dedup은 Telegram immutable metadata의
canonical serialization을 기준으로 계산한다. 메시지 본문, body HMAC, key_id, route policy, task type은
Task Identity 입력이 아니다.

- HMAC key가 회전해도 root_task_id는 불변이다.
- HMAC key가 회전해도 동일 Telegram message의 logical event_id는 불변이다.
- 이전 key를 사용할 수 없어도 Task와 event 조회는 가능하다.
- fingerprint가 UNKNOWN이어도 Task Identity를 생성할 수 있다.
- HMAC rotation으로 cross-bot dedup 결과가 달라지지 않는다.
- HMAC은 본문 관찰 fingerprint일 뿐 실행 identity가 아니다.

다음과 같은 설계는 금지한다.

task_id = hash(body_hmac)
event_id = hash(key_id + body_hmac)

## 10. Disk-full 상태·복구·승인

상태별 동작:

- NORMAL: 정상 관찰·저장.
- SOFT_LIMIT: retention dry-run, 닫힌 JSONL segment 후보 계산, health warning을 수행한다. Legacy Telegram은
  계속하며 자동 purge는 하지 않는다.
- HARD_LIMIT: 신규 Shadow event 수락을 중단하고 dropped_disk_budget을 증가시킨다. DB·JSONL 추가 write를
  중단하고 Observer를 degraded 또는 No-op으로 전환한다. Legacy Telegram은 계속하며 로그를 rate-limit한다.
- READ_ONLY_DEGRADED: 상태 조회·복구 판단만 허용하고 Shadow write는 하지 않는다.
- RECOVERING: 승인된 복구 절차와 offline smoke test를 진행하는 상태다.

Token, Provider state, 다른 운영 파일, home 전체, 운영 state root는 자동 삭제하지 않는다.

Disk-full 복구 절차:

1. Shadow를 OFF로 전환할 계획을 수립한다.
2. 대상 Bot 재시작 승인을 받는다.
3. Shadow writer가 중지됐는지 확인한다.
4. status와 retention-dry-run을 실행한다.
5. 삭제 후보·크기·보존 제외 항목을 검토한다.
6. 사용자 승인을 받는다.
7. 승인된 retention-execute 또는 닫힌 JSONL segment 삭제를 실행한다.
8. verify를 실행한다.
9. root 크기와 SQLite 상태를 재확인한다.
10. RECOVERING 상태로 전환한다.
11. 임시 write test 또는 offline smoke test를 실행한다.
12. 사용자 승인 후에만 Canary를 재활성화한다.

다음은 모두 명시적 사용자 승인 대상이다: Feature Flag OFF 적용과 Bot 재시작, retention-execute,
JSONL segment 실제 삭제, purge-all-execute, 손상 DB quarantine, 새 DB 생성, 새 HMAC key 생성,
Canary 재활성화.

자동으로 home·운영 state·Token·Provider 로그·SQLite·active JSONL을 정리하지 않는다. 삭제는 검증된
maintenance subcommand만 사용하며, 광범위한 shell 삭제·재귀 권한 변경·무조건적인 파일 탐색 삭제는 사용하지 않는다.

## 11. 삭제 안전 계약

purge-all-dry-run이 기본이다. purge-all-execute는 명시적 --root, root 검증, Shadow OFF, Observer 중지,
maintenance lock, 사용자 승인을 모두 요구한다.

- /, home 전체, 운영 state·Token·Provider state·Telegram delivery/outbox는 거부한다.
- root 밖 경로와 symlink traversal은 거부한다.
- 대상 수와 크기를 먼저 표시한다.
- 부분 실패 시 남은 파일 목록만 기록하고 추가 자동 삭제를 하지 않는다.
- 실제 삭제 후 verify로 결과를 확인한다.
- 개별 task/message 선택 삭제는 이 단계의 계약에 포함하지 않는다.

## 12. 장애·복구 Runbook

- SQLite 손상: DB를 덮어쓰거나 authoritative 복구 원본으로 대체하지 않는다. Observer를 degraded/OFF로
  전환하고, quarantine·원인 분석·새 DB 생성은 승인 대상이다.
- JSONL 손상: segment를 quarantine한다. JSONL을 authoritative 복구 원본으로 간주하지 않으며 SQLite
  event와 outbox를 기준으로 재생성 가능 범위를 판단한다.
- HMAC key 손실: body fingerprint를 UNKNOWN으로 기록하고 기존 event를 유지한다. 새 key 생성은 승인 대상이며
  Task Identity에는 영향이 없다.
- Process crash: FLUSHING outbox를 pending으로 복구하고 lease·lock 만료, active segment, temporary file을
  점검한다.
- 모든 장애에서 Legacy Telegram을 계속 운영하고 오류 로그를 rate-limit한다.

## 13. Canary 중단·복구

첫 Canary는 Antigravity 단일 Bot만 대상으로 한다. Codex와 Claude는 OFF로 유지하고, 예시 후보 root
~/.edge-agent/phase1a-shadow/antigravity-canary와 독립 key path는 활성화 승인 전 생성하지 않는다.
Canary에는 별도 0600 key, 0700 root, retention·rotation·disk budget 설정이 필요하다.

중앙 Claim·Provider 제어·Shadow Telegram output은 계속 비활성이다. 다음이면 즉시 OFF를 준비한다:
hard limit, 반복 DB write failure, 반복 JSONL rotation failure, HMAC key 검증 실패, root 권한 변화,
symlink 감지, worker crash, Legacy handler latency 초과, polling conflict, 중복 event 또는 응답 오류.

Canary 중단 절차:

Antigravity Shadow Flag OFF
→ Antigravity만 재시작
→ Shadow worker 0 확인
→ Legacy Telegram E2E 확인
→ disk·DB·outbox 점검
→ 원인별 사용자 승인 후 복구

설정 rollback은 Flag OFF + Antigravity 재시작이다. 코드 rollback은 승인된 Runtime rollback patch 적용 후
영향받은 Bot을 순차 재시작하는 별도 절차다. Disk-full만으로 Runtime code rollback을 자동 실행하지 않는다.

Canary 성공 기준은 Legacy 응답 성공률 보존, 추가 latency 허용범위, queue drop, disk 증가량, SQLite busy,
pending outbox, JSONL rotation, 민감정보 비저장, logical event dedup 정확성, PID 안정성, 즉시 OFF 복귀를
모두 만족하는 것이다. 최소 24시간과 100개 관찰 이벤트 중 더 긴 기간을 사용하며 실제 운영량 측정 후 재승인한다.

## 14. 오프라인·승인 경계

현재 문서는 운영 적용 전 오프라인 검증 계약이다. retention·rotation·disk budget·key lifecycle·삭제·복구
절차를 offline test와 사람 검토로 확정하기 전에는 Canary를 활성화하지 않는다. 실제 Telegram 메시지 관찰,
Provider 호출, Runtime patch, LaunchAgent 변경, 운영 root·HMAC key 생성은 이 문서만으로 승인되지 않는다.
