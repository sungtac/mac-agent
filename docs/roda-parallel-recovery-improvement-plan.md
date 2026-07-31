# Roda 병렬 처리 / 자동복구 개선 계획

**계기**: 2026-08-01 오전 2:57~3:04 사이 실제 발생한 장애 체인 —
1. `❌ Codex 작업공간 생성 오류: .../parallel/locks/repo-244afce648d1247d626837c48f351a3d.lock`
2. Roda가 300초 내 완료/오류 이벤트 없음(`no_response`) 감지
3. Codex 자동복구(진단→수정→병합) 시도했으나 "main에 추적 파일 변경이 있어 자동 병합하지 않았습니다"로 실패
4. 병합 실패했으므로 문제 봇 재처리 지시 없이 종료 — **감지는 됐지만 고쳐지지 않고 끝남**

아래는 코드(`bin/telegram-agent-bot.py`, `bin/roda-telegram-health-monitor.py`, `bin/edge_agent_parallel_locks.py`, `bin/edge_agent_parallel_audit.py`)를 직접 읽고 확인한 근본 원인과, 그에 대응하는 개선안이다.

## 근본 원인 6가지

### G1. 락 재시도 창이 너무 짧다
`telegram-agent-bot.py:106-107` — `WORKTREE_LOCK_RETRIES=5`, `WORKTREE_LOCK_RETRY_SECONDS=1`. 총 대기시간 약 5초. `repository_lifecycle_lock`은 저장소 단위 배타 락이라(`edge_agent_parallel_locks.py`), 같은 저장소를 대상으로 여러 역할(Telegram Codex, Discord codex-bot, Roda 복구 worktree 생성)이 동시에 worktree를 만들려 하면 5초 안에 안 풀릴 수 있다. 결과: 사용자에게 raw 예외 메시지(`lock` 파일 경로 그대로)가 그대로 노출됨.

### G2. 복구 경로가 "병렬 가능" 설계를 스스로 어긴다
`edge_agent_parallel_locks.py` 상단 주석: "저장소 전체 실행 락은 쓰지 않는다, 안 그러면 겹치지 않는 다른 worktree들이 병렬로 못 돈다." 그런데 `roda-telegram-health-monitor.py:413`의 자동복구는 진단용 worktree를 만들 때 바로 그 저장소 전체 `repository_lifecycle_lock`을 잡는다. 지금은 `git worktree add` 한 줄이라 짧게 끝나지만, 저장소 단위 락을 쓰는 지점이 하나 더 늘어난 것 자체가 G1의 경합 빈도를 높인다.

### G3. 병합 실패가 막다른 길이다 (사용자가 지적한 핵심 문제)
`roda-telegram-health-monitor.py:477-479`: main에 추적된 변경(untracked 제외)이 있으면 병합을 포기하고 문자열만 반환한다. 그 뒤:
- 실패한 repair commit은 `REPAIR_ROOT/<fingerprint>` worktree에 그대로 방치된다.
- main이 나중에 깨끗해져도 **아무도 재시도하지 않는다** — 재시도 큐도, 다음 폴링 사이클에서의 재확인도 없다.
- `_format_repair_result`는 실패 시 "자동 수정이 완료되지 않았으므로 문제 봇에 재처리를 지시하지 않습니다"라고만 알리고 끝. 사람이 그 메시지를 보고 직접 worktree를 찾아 수동 병합해야 하는데, worktree 경로/커밋 해시가 알림에 없어 그마저도 번거롭다.

### G4. main의 추적 변경 자체가 상시 차단 요인
지금 이 순간 `mac-agent`의 main에 `bin/roda-telegram-health-monitor.py`, `tests/test_roda_telegram_health_monitor.py`가 커밋되지 않은 채 올라가 있다(이전 세션 작업물, usage-limit 정규식 확장 패치). 이게 있는 한 **앞으로의 모든 자동복구 병합 시도가 항상 실패**한다. G3의 안전장치(추적 변경 있으면 병합 안 함)는 올바른 판단이지만, 그 원인 상태 자체가 방치되고 있다는 걸 아무도 알리지 않는다.

### G5. FileReservation에 TTL/heartbeat가 없다
`edge_agent_parallel_locks.py`의 `FileReservation`은 `active`/`released` 두 상태만 있고 만료 개념이 없다. 작업이 크래시해서 `release()`를 못 부르면 그 파일 경로/의존성 키에 대한 예약이 **영구히** 남아 이후 겹치는 다른 작업을 계속 `ReservationConflict`로 막는다. `edge_agent_parallel_audit.py`는 worktree manifest ↔ git worktree 불일치만 감사하고, 이 reservation registry의 stale 항목은 감사 대상이 아니다.

### G6. 복구가 단발성(single-shot) 설계
진단 1회 → 병합 1회 → 실패하면 종료. 백오프도, 재시도 루프도, "다음에 자동으로 다시 시도함" 경로도 없다. 감지(300초 no_response, usage_watch 등)는 이미 꽤 정교한데, 복구 쪽만 재시도 개념이 빠져있어 이 프로젝트의 "탐지는 잘하는데 고치질 못한다"는 인상을 만든다.

## 개선 계획 (우선순위순)

### P0 — 즉시 (수동, 5분)
main의 추적 변경 커밋/스태시 처리. 이게 남아있는 한 P1 이하를 구현해도 검증이 안 된다.
```
cd /Users/edge_ai/mac-agent
git add bin/roda-telegram-health-monitor.py tests/test_roda_telegram_health_monitor.py
git commit -m "..."   # 또는 git stash — 사용자 확인 필요, 내용이 이전 세션 미완성 작업이라 판단 필요
```

### P1 — main 청결도를 1급 헬스 신호로 승격
`roda-telegram-health-monitor.py`에 새 감지 항목 추가: 폴링 사이클마다 `git -C SOURCE_REPO status --porcelain`에 추적 변경이 있으면 `code="main_dirty"` 알림을 (스팸 방지를 위해) 1회/일 정도로 발송. "자동복구가 막혀있다"는 사실을 사람이 놓치지 않게 한다. G4 재발 방지.

### P2 — 실패한 복구를 재시도 큐로 전환
`_run_codex_repair_impl`이 G3 경로(main dirty / merge conflict)로 실패하면:
- `state["pending_merges"][fingerprint] = {"worktree": ..., "repair_commit": ..., "role": ..., "queued_at": ...}` 형태로 상태 파일에 기록.
- 다음 폴링 사이클마다(또는 별도의 가벼운 체크) main이 깨끗해졌는지 확인 → 깨끗하면 그 저장된 `repair_commit`으로 병합만 재시도 (재진단 불필요, 이미 만든 커밋 재사용).
- 성공하면 그때 서비스 재기동 + "재처리하세요" 알림. TTL(예: 24h) 지나면 큐에서 빼고 "수동 병합 필요: worktree=<path>, commit=<hash>" 알림으로 전환.
- 이렇게 하면 G3의 "병합 실패=끝"이 "병합 실패=대기열, 조건 충족시 자동 재개"로 바뀐다.

### P3 — 락 재시도 파라미터 현실화 + 대기 가시성
`WORKTREE_LOCK_RETRIES`/`WORKTREE_LOCK_RETRY_SECONDS` 기본값을 상향 조정(예: 지수 백오프로 총 30~60초)하고, 재시도 중임을 이미 있는 `_notify_waiting` 패턴처럼 사용자에게 진행 상태로 보여준다(현재는 로그에만 남고 Telegram 사용자는 그냥 기다림). 5초 만에 포기하고 락 파일 경로를 그대로 노출하는 대신, 최소한 "다른 작업 처리 중, 잠시 후 재시도"로 사용자 경험 개선.

### P4 — 복구 경로의 락 스코프 축소
`_run_codex_repair_impl`의 진단 worktree 생성을 `repository_lifecycle_lock` 대신, 가능하면 `task_lock`(fingerprint 단위)만 쓰도록 검토. 저장소 전체 배타 락을 잡는 지점을 하나 줄여 G1의 경합 확률을 낮춘다. (단, `git worktree add`가 내부적으로 `.git/worktrees` 메타데이터를 건드리므로 완전히 락-프리로 만들기는 어려움 — 최소한 lifecycle lock 보유 시간을 지금보다 더 줄이는 방향으로 검토.)

### P5 — Reservation 감사/TTL
`edge_agent_parallel_audit.py`에 reservation registry 감사 추가(각 `active` 레코드에 `created_at` 대비 경과시간 계산, 임계값 초과 시 `stale_reservation` finding). 지금 이 감사는 read-only 원칙을 지키고 있으니, 자동 해제는 하지 말고 finding만 내고 사람이 승인 후 해제하는 흐름 유지 (기존 설계 철학과 일치).

## 이 계획이 "병렬 처리" 관점에서 노리는 것

지금 구조는 감지(Roda 헬스 모니터)는 이미 촘촘한데, 복구는 "한 번 해보고 안 되면 끝"이라 병렬로 여러 에이전트가 같은 저장소를 오갈 때 실패가 누적되기만 한다. P1(가시성)+P2(재시도 큐)만 넣어도 "탐지 잘하고 고치는 것"까지 완성되고, P3~P5는 애초에 그 실패가 덜 발생하게 만드는 예방 조치다. 구현 순서는 P0→P1→P2를 먼저 하고, P3~P5는 실제로 락 경합이 재발하는지 로그로 확인한 뒤 착수하는 걸 권장.
