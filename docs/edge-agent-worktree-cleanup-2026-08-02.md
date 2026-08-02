# Edge Agent worktree·산출물 정리 기록

작성일: 2026-08-02

## 현재 분류

- 등록 worktree: 72개
- clean: 19개
- dirty: 53개
- missing: 0개
- 삭제·prune 실행: 없음

dirty worktree는 Telegram task, health repair, integration, dashboard 등 서로
다른 작업 주체가 소유할 수 있으므로 이번 단계에서 삭제하거나 reset하지 않는다.
missing worktree가 없으므로 `git worktree prune`도 실행할 필요가 없다.

## 산출물 정책

- `.pytest_cache/`: 로컬 테스트 캐시로 분류하고 `.gitignore`에 추가했다.
- `.verify/runs/`: 검증 실행 산출물로 분류하고 `.gitignore`에 추가했다. 기존
  `SKILL-CLEANUP-20260801` 자료는 삭제하지 않고 보존한다.
- `__pycache__/`: 기존 ignore 정책을 유지한다.

## 다음 삭제 조건

특정 worktree를 삭제하려면 소유 task가 종료 상태인지, pending delivery/repair가
없는지, 현재 활성 LaunchAgent나 세션이 참조하지 않는지를 개별 확인한 뒤 해당
경로만 recoverable 방식으로 제거한다. 전체 worktree 일괄 삭제는 금지한다.
