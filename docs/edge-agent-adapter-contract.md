# Team OS ↔ 엣지 에이전트 어댑터 계약

상태: v1 데이터 계약 구현, 런타임 연결 전

## 소유권

- Team OS: 목표·상위 역할·위험도·사람 승인
- 엣지 에이전트: provider 선택 실행·sandbox·worktree·락·검증
- 어댑터: 양쪽 내부 모듈을 직접 호출하지 않고 DTO만 전달

## 요청

`team_os.edge_agent_request.v1` 요청에는 `request_id`, `task_id`,
`logical_session_id`, `objective`, `risk_level`, 허용 파일, 기준 commit,
완료 게이트, `approval_ref`가 포함된다.

고위험 작업(`send`, `delete`, `system`, `secret`)은 승인 참조 없이
`dispatch_allowed=true`가 될 수 없다. `secret` 작업은 실행 허용하지 않는다.

## 결과

`edge_agent.team_os_result.v1` 결과는 상태, 변경 파일, 검증 단계,
멱등 이벤트 키, provider 이름, 사용량 snapshot 참조, 증거 참조,
오류 코드와 다음 행동을 반환한다.

`passed`는 provider의 exit code만으로 만들 수 없다. 검증 단계·멱등 이벤트 키·
증거 참조가 모두 있어야 한다.

## 금지

- 토큰·비밀번호·쿠키·API key 원문 전달
- 전체 대화 transcript 전달
- Team OS 내부 router/approval 객체 직접 import
- DTO만으로 실행 권한 부여

현재는 계약과 token-free 테스트만 구현했으며, 실제 어댑터·Telegram·Discord·
launchd 연결은 다음 단계에서 별도 검증 후 진행한다.
