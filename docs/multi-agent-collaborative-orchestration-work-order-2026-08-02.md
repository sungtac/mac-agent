# 4인 협업형 Edge Agent 구현 작업지시서

상태: 검토 요청 전 초안
작성일: 2026-08-02
대상: Claude, Codex, Antigravity, Roda와 Telegram/터미널 공통 런타임

## 1. 작업 목적

사용자의 목표를 하나의 단순 질의응답으로 처리하지 않고, 서로 독립된 정체성과
판단을 가진 4개 에이전트가 단체방에서 조사·토론·위임·검증·통합하는 협업형
멀티에이전트 시스템을 구현한다.

터미널이 기능과 운영 규칙의 기준(source of truth)이며, Telegram은 같은 라우팅,
도구 탐색, 승인, 세션, 위임, 검증 기능을 제공해야 한다.

## 2. 절대 원칙

1. 사용자의 목표를 사전에 축소하지 않는다.
2. 능력을 모르면 먼저 capability discovery를 수행한다. 실제 확인 전에는 “할 수
   없다”고 단정하지 않는다.
3. 요약은 토큰 절약 수단이지 정확도 저하 수단이 아니다. 기본은 균형 요약이며,
   불확실성·위험도·작업 복잡도에 따라 원문 범위를 확장한다.
4. 확인하지 않은 실행·검색·가격·수익·완료 상태를 만들어내지 않는다.
5. 일반 그룹 발화는 4개 에이전트가 참여할 수 있어야 한다. 특정 에이전트를 직접
   호출한 발화는 해당 에이전트가 중심이 된다.
6. 에이전트 메시지를 무조건 차단하지 않는다. 에이전트 간 메시지는 provenance,
   task/session ID, hop 수, 라운드 예산을 가진 내부 협업 메시지로 처리한다.
7. 외부 전송·계정 변경·서비스 변경·파일 삭제·유료 실행은 승인 규칙을 따른다.

## 3. 단체방 라우팅 계약

### 3.1 사람의 메시지

| 입력 유형 | 기본 처리 |
|---|---|
| `안녕하세요`, 일반 인사·의견 | 4개 에이전트가 각각 자연스럽게 응답 가능 |
| `코덱스야 ...` / `@edgeai_macmini_bot ...` | Codex 중심. 다른 에이전트는 자동 개입하지 않음 |
| `로다야 ...` | Roda 중심 |
| `안티야 ...` | Antigravity 중심 |
| `클로드야 ...` | Claude 중심 |
| `각자`, `얘들아`, `모두` | 명시적 전체 fan-out |
| `안티와 로다가 논의해` | 지정된 에이전트 간 협업 |
| `너희들이 논의해`, `방법을 찾아줘` | 4인 deliberation session 생성 |

직접 호출과 전체 호출이 문장 안에서 충돌하면 명시적인 이름·mention을 우선하고,
전체 참여가 필요한 경우 사용자의 표현을 확인하거나 deliberation으로 승격한다.

부분 지정 발화는 지정된 에이전트만 활성화한다. 예를 들어 `안티와 로다가
논의해`는 Antigravity와 Roda만 작업 노드로 만들고 Claude·Codex는 관찰자 또는
명시적으로 위임받은 reviewer가 되기 전까지 응답하지 않는다. `안티야, 로다야`
처럼 이름을 나열한 경우도 같은 규칙을 적용한다.

### 3.2 에이전트의 메시지

다음 메시지는 일반 사용자 메시지와 구분한다.

```json
{
  "kind": "agent_message",
  "session_id": "...",
  "task_id": "...",
  "from": "roda",
  "to": ["codex"],
  "trust_domain": "telegram-internal",
  "key_id": "agent-roda-v1",
  "signature": "...",
  "source_event_id": "...",
  "purpose": "request_review",
  "summary": "...",
  "evidence_refs": ["..."],
  "hop": 1,
  "round": 1,
  "requires_user_report": false
}
```

사람에게 바로 보고하지 않고, 지정된 상대 에이전트가 자신의 판단으로 응답하고
필요하면 다음 작업을 위임한다. 단, 동일 메시지 반복·순환 대화·무제한 토론은
deduplication key, 최대 hop, 최대 round, deadline으로 차단한다.

agent_message는 Telegram의 일반 수신 메시지와 섞지 않는다. 내부 event bus와
Telegram egress를 분리하고, 사용자에게 보일 내부 대화만 provenance가 붙은
응답으로 별도 전송한다. Telegram으로 나간 봇 메시지가 다시 작업 ingress가 되지
않도록 `origin=agent`, `source_event_id`, `handled_by`를 확인하는 재진입 차단을
필수로 한다. `from`, `trust_domain`, `key_id`, `signature`는 ingress에서
허용된 에이전트 등록정보와 대조·검증하며, 검증되지 않은 내부 메시지는 작업·승인·
권한 변경에 사용할 수 없다.

4개 에이전트의 응답은 중앙 egress queue를 거친다. queue는 chat별 rate limit,
순서, 중복, 실패 재시도, backpressure를 관리하며, 동시 응답이 Telegram API
제한을 넘지 않도록 한다.

## 4. 에이전트 역할과 책임

- Claude: 기본 coordinator 역할, 요구사항·목표·의존성·최종 통합
- Codex: 구현·자동화·기술 검증·재현 가능한 실행
- Antigravity: 독립 조사·레드팀·반대 가설·누락 검증
- Roda: 로컬 대화·현실성 검토·사용자 관점·운영 상태 관찰

역할은 우선 책임이지 고정 능력 제한이 아니다. 요청에 따라 어떤 에이전트든
조사·분석·아이디어 제안·검토를 수행할 수 있으며, 실제 도구와 권한을 먼저
확인한다. coordinator는 별도 다섯 번째 모델이 아니라 event bus·task graph·
egress를 관리하는 논리적 control-plane 역할이다. Claude가 peer 의견도 내는
경우에는 1차 의견을 먼저 봉인한 뒤 통합 단계에서만 coordinator 권한을 사용하며,
자기 결과를 독립 reviewer로 판정하지 않는다.

## 5. 계층형 위임 모델

```text
사용자 목표
  ↓
논리적 coordinator가 목표·완료조건·위험·작업 그래프 작성
  ↓
4개 에이전트가 역할별 1차 과제 수행
  ↓
각 에이전트가 서브에이전트에게 조사·계산·추출·검증 위임
  ↓
에이전트별 결과 취합·자기검증
  ↓
독립 reviewer가 충돌·누락·근거를 검토
  ↓
상위 coordinator가 통합·우선순위·실행계획 결정
  ↓
사용자에게 결론·근거·선택지·다음 행동 보고
```

4개 에이전트는 1차 의견 단계에서 동등한 peer로 취급한다. coordinator는
작업 그래프 생성·상태 관리·통합을 담당하지만, 자기 peer 의견을 독립 검토
결과로 재사용하지 않는다. coordinator가 일시적으로 중단되면 저장된 graph와
result packet을 기준으로 지정된 fallback coordinator가 이어받는다.

각 결과는 다음 계약을 사용한다.

```text
결론:
근거:
신뢰도:
확인하지 못한 점:
대안:
다음 행동:
원문/로그 참조:
```

## 6. 사고 트리와 deliberation

예시 요청:

> 30일 안에 100만원의 수익을 만들고 싶다. 가능한 방법을 논의해줘.

실행 트리는 다음처럼 확장한다.

1. 목표: 30일, 순수익 100만원, 사용 가능한 시간·자본·기술 확인
2. 조사: 시장 수요·경쟁·가격·채널·법적 제한 조사
3. 전략 후보: 서비스 판매, 디지털 상품, 자동화, 콘텐츠, 제휴 등
4. 모델별 독립 평가: 실행 난이도·수익 가능성·시간·위험·검증 방법
5. 반대 검토: 과장된 수익 가정, 숨은 비용, 고객 확보 실패 조건
6. 소규모 실험: 24~72시간 검증 가능한 제안·가격·고객 접점 설계
7. 통합: 1순위·2순위·중단 기준·일일 실행계획 결정
8. 피드백: 실제 반응과 지표를 다음 라운드에 반영

작업 그래프의 초기 기본값은 `max_subagent_depth=2`,
`max_deliberation_rounds=3`, `max_active_tasks_per_session=8`,
`task_token_budget=4000`, `session_token_budget=24000`,
`cache_ttl_default=15m`으로 둔다. 모든 값은 설정으로 조정할 수 있지만 hard cap을
넘을 수 없다. 중단·취소·timeout이 발생하면
하위 작업을 cascade 취소하고, 이미 완료된 결과만 보존한다.

## 7. 컨텍스트·토큰 운영 원칙

- 전체 대화 대신 원문 파일, 균형 요약, 근거 위치, 버전·해시를 전달한다.
- 단순 라우팅은 짧은 패킷, 일반 조사는 균형 요약, 실행·고위험 작업은 관련 원문을
  직접 확인한다.
- 모델별로 필요한 context view를 만든다. 조사자는 원자료, 구현자는 명세·파일,
  reviewer는 diff·테스트·근거를 우선 받는다.
- 결과가 불명확하면 원문 전체를 재전달하지 않고 필요한 파일·문단만 확장한다.
- 동일한 조사·요약·도구 결과는 캐시하고 변경분만 전달한다.
- 모델 호출에는 작업별 token budget, 최대 라운드, 종료 조건을 둔다.
- 초기 예산값은 작업당 4,000 토큰, session당 24,000 토큰이며, 4인 일반 인사는
  에이전트별 300 토큰 hard cap과 짧은 응답 profile을 사용한다.
- 동시 작업은 session당 8개를 hard cap으로 하며, source 변경 캐시는 기본 15분,
  변동성 높은 외부 자료는 15분, 안정적인 문서 해시는 24시간 TTL을 사용한다.
- 기존 `workflows/lib/coach-headroom.sh`, `usage-advisor.sh`,
  `route-dispatch.sh`의 사용량·라우팅 정책과 `bin/verify-task-orchestrator.py`
  검증 게이트를 새 오케스트레이터의 선행 정책으로 계승한다. 새 흐름이 이
  정책을 우회하지 않으며, 예산 부족 시 작업을 축소·대기·사용자 승인으로
  전환한다.
- 토큰 최소화보다 정확도·검증 가능성·사용자 목표 달성을 우선한다.
- 캐시는 source version, API freshness, TTL, semantic invalidation 기준을 갖는다.
- 단순 인사도 사용자가 전체에게 말한 경우 4개 에이전트 참여를 유지하되, 짧은
  응답 profile과 queue batching을 사용해 비용·전송량을 제어한다.

## 8. capability-first 실행

알려지지 않은 요청도 다음 순서로 처리한다.

1. 목표와 제약조건 파악
2. 현재 실행 파일·도구·endpoint·인증 상태·자료 확인
3. 사용 가능한 경로를 에이전트별로 분배
4. 도구 실행 결과를 근거와 함께 취합
5. 도구가 없거나 실패한 경우에만 구체적인 제한을 보고

외부 자료나 한 에이전트의 결과가 다른 에이전트의 지시가 되는 경우에도 원문
콘텐츠와 실행 지시를 분리한다. prompt injection이 포함될 수 있는 자료는
untrusted evidence로 표시한다. ingress에서 원문·지시·메타데이터를 분리하고,
도구 반환 직후 untrusted 표식을 붙이며, 저장·전달 직전에 secret sanitization과
권한 재검사를 수행한다. 내부 메시지가 승인·권한·sandbox 경계를 우회하지
못하도록 검증되지 않은 provenance는 실행 지시로 승격하지 않는다.

deliberation 중 승인 대기 상태가 되면 전체 그래프를 `waiting_approval`로 전환하고,
승인 대상 task ID와 사용자 결정을 저장한다. 승인·거절·취소는 해당 session의
모든 대기 노드에 전파하며, 에이전트가 내부적으로 승인했다고 간주하지 못하게 한다.

날씨처럼 실시간 정보가 필요한 요청은 모델의 기억이나 추측이 아니라 실제 조회
어댑터를 우선 사용한다. 가격·여행·시장·주식·플랫폼 알고리즘처럼 변동성이 큰
주제는 최신 자료와 출처를 확인한다.

동적 가격·계정·위치·협상 전략은 합법적이고 서비스 약관을 준수하는 범위에서만
다룬다. 사기, 우회, 접근제어 회피, 허위 신분, 조작적 자동화는 제외한다.

## 9. 구현 작업 순서

### Phase 0: 자료조사와 기준선

- 터미널 진입점과 Telegram canonical service를 식별한다.
- `mac-agent`와 `engine-repo`의 역할·소유권·LaunchAgent·token owner를 정리한다.
- 현재 라우팅 문서와 실제 코드의 차이를 목록화한다.
- 현재 테스트·런타임 상태·dirty 변경을 checkpoint로 기록한다.
- 현재 선행 변경인 `bin/edge_agent_ingress.py`, `bin/edge_agent_secure_paths.py`,
  `bin/weather_adapter.py`와 관련 테스트는 baseline으로 별도 기록한다. 이
  변경을 본 작업의 Phase 2 완료로 간주하지 않으며, Phase 1 설계 승인 후
  계약 적합성을 재검증한다.

### Phase 1: 제품·아키텍처 기획

- 공통 ingress/event envelope 계약 설계
- canonical event bus와 chat별 egress queue 설계
- 사람 메시지·에이전트 메시지·deliberation session 구분
- role assignment, subtask, result packet, evidence reference 설계
- context view와 adaptive expansion 정책 설계
- loop·중복·hop·round·timeout·approval 정책 설계
- agent identity 서명·key rotation·trust domain 검증과 부분 지정 라우팅 설계
- 재시작 복구, 전역 취소, deadlock/tie-break, approval propagation 설계
- LaunchAgent·state store·IPC single-writer와 lock 소유권 설계
- 기존 사용량·라우팅·`verify-task-orchestrator.py` 게이트와의 연동 설계
- 터미널과 Telegram parity acceptance matrix 작성

### Phase 2: 코딩

- 공통 라우터와 provenance-aware message envelope 구현
- Claude·Codex canonical engine·Antigravity·Roda에 동일 계약 연결
- 4인 fan-out과 특정 에이전트 직접 호출 구현
- 부분 지정 fan-out과 agent identity/persona 보존 구현
- 에이전트 간 대화 및 deliberation session 구현
- 서브에이전트 위임·결과 취합·상위 통합 구현
- 날씨·검색·문서·코딩 등 capability adapter registry 연결
- 균형 요약·원문 확장·근거 참조 구현
- 작업 그래프·세션·delivery·재시작·관찰 로그 연결

### Phase 3: 코드리뷰와 독립검증

- 필수 게이트: Claude의 아키텍처·요구사항·운영 parity 검토
- 필수 게이트: Antigravity의 라우팅 누수·무한 루프·권한·환각·실패 경로 레드팀 검토
- 선택 검토: Codex의 실제 diff·테스트·재현성 검토
- 선택 검토: Roda의 사용자 대화 흐름·자연스러움·내부 협업 문체 검토

작성자와 reviewer는 동일 모델로 고정하지 않는다. 리뷰 결과에는 파일·심볼·근거·
차단 여부·필요한 테스트를 포함한다.

### Phase 4: 검증과 점진적 적용

- 순수 라우팅 unit test
- 4개 에이전트 fan-out test
- 직접 호출 exclusivity test
- 부분 지정 발화 라우팅 test
- 4개 모델의 identity/persona·판단 독립성 유지 test
- agent-to-agent message loop test
- deliberation task graph test
- context ambiguity expansion test
- capability discovery와 weather live/mock test
- delivery retry·session resume·restart test
- Telegram rate limit/backpressure/ordering test
- agent_message circular-loop circuit breaker test
- agent_message 발신자 위조·서명·key rotation test
- coordinator/peer 순서 충돌 및 자기검토 금지 regression test
- task/session token budget·동시성 hard cap 초과 차단 test
- prompt injection과 secret sanitization boundary test
- approval 대기·거절·전역 취소·재시작 복구 test
- 실제 Telegram canary는 별도 승인 후 수행

## 10. 완료 기준

다음 조건을 모두 만족해야 완료로 판정한다.

- 터미널과 Telegram의 기능·라우팅·승인 계약이 동일하다.
- `안녕하세요` 같은 전체 발화에 4개 에이전트가 참여한다.
- 직접 호출은 의도하지 않은 에이전트를 깨우지 않는다.
- 부분 지정 호출은 지정된 에이전트만 활성화한다.
- 에이전트가 다른 에이전트에게 근거 있는 작업 요청을 보낼 수 있다.
- 4개 에이전트의 identity·persona·독립 판단이 최종 통합 과정에서 보존된다.
- 서브에이전트 결과가 상위 레벨에 구조적으로 취합된다.
- 불확실한 요약은 필요한 원문만 추가로 확장한다.
- 능력을 사전 제한하지 않고 capability discovery 후 가능한 경로를 시도한다.
- 반복·순환·중복 응답은 예산과 provenance로 제어된다.
- 모든 최종 결론에 근거·신뢰도·미해결점이 남는다.
- 전체 자동화 테스트와 독립 코드리뷰가 통과한다.
- 필수 독립 코드리뷰(Claude·Antigravity)가 `pass`이고, 차단 이슈가 없다.
- live service가 실제 canonical LaunchAgent 기준으로 running이며, 직접 확인한
  상태와 문서가 일치한다.

## 11. 검토 요청

Claude와 Antigravity는 필수 게이트 reviewer로 이 문서를 기준으로 다음 형식의
독립 검토를 수행한다. Codex와 Roda는 별도 승인된 경우 선택 reviewer로 참여한다.

```text
reviewer:
scope:
blocking_issues:
missing_requirements:
architecture_risks:
security_and_safety_risks:
token_and_context_risks:
test_gaps:
recommended_changes:
verdict: pass | changes_required
```

리뷰어는 코드를 수정하지 말고, 문서의 구현 가능성·누락·충돌·검증 가능성만
판정한다.

## 12. 2026-08-02 검토 상태

### 안티그래비티 검토

- 판정: `changes_required`
- 주요 차단 항목: Telegram 송신 큐·속도 제한·순서 보장·백프레셔, 에이전트 메시지의 재진입/무한루프 차단, 진행 중 승인 상태의 동기화와 취소 전파
- 추가 누락 항목: 재시작 후 세션·작업 그래프·중복 제거 상태 복구, 전역 취소, 교착상태 및 의견 충돌 타이브레이커, 에이전트 간 권한 경계
- 보안 항목: 프롬프트 인젝션이 에이전트 메시지를 통해 전파되지 않도록 신뢰 경계를 분리하고, 증거·로그에 비밀정보가 남지 않도록 정제
- 토큰·성능 항목: 4개 모델의 불필요한 잡담과 전체 대화록의 반복 전달을 줄이되, 일반 인사처럼 사용자가 명시적으로 모두에게 말한 경우에는 4개 모델이 참여한다. 이때 짧은 응답 프로파일과 배치·속도 제한을 사용한다.
- 검토자가 제안한 “단순 인사는 단일 응답으로 제한” 방안은 사용자의 요구(모두에게 인사하면 4개 모델 모두 응답)와 충돌하므로 채택하지 않는다.

### 클로드 검토

- 판정: `changes_required`
- 실제 검토 완료: `/Users/edge_ai/.local/bin/claude`를 직접 호출했고, 인증 상태는
  `loggedIn: true`, `authMethod: claude.ai`, `subscriptionType: pro`였다.
- 최초 실패 원인: `claude` 명령이 로컬 terminal shim을 거치며 최신 CLI가 지원하지
  않는 `--append-system-prompt` 인자를 주입했다. 인증 부재로 단정하지 않고 실제
  바이너리로 우회해 재검토했다.
- 재발 방지: `bin/edge-agent-terminal-shim`이 `auth`, `mcp`, `help`, `version` 등
  관리 명령에는 컨텍스트 인자를 붙이지 않고 원본 CLI에 그대로 전달하도록 수정했다.
  수정 후 shim 경로의 `claude auth status`와 `claude --version`을 실측했다.
- 주요 지적: coordinator와 Claude peer 역할 충돌, 필수 리뷰어 범위 불일치,
  agent message 발신자 위조 방지 누락, 기존 사용량·검증 게이트 연동 누락, 부분
  지정 라우팅·정체성 유지 테스트 누락, 수치형 token/concurrency budget 부재.
- 반영: 위 항목을 역할 계약, 서명 필드, 부분 라우팅, 기존 게이트, persona
  acceptance test, 초기 hard cap 및 baseline 기록 요구사항으로 본 문서에 추가했다.

이번 검토 결과를 반영해 본 작업지시서는 중앙 이벤트 버스와 Telegram 송신 큐의 분리, 메시지 출처·홉·라운드 기반 재진입 방지, 승인·취소·재시작 복구, 하위 에이전트 깊이·라운드·동시성 제한, 프롬프트 인젝션·비밀정보 정제, 속도 제한·백프레셔·회복성 테스트를 필수 범위로 포함한다.

### 최종 구현 재검토 및 적용 상태 (2026-08-02)

- 구현 반영: 일반 무주소 발화는 4개 역할에 fan-out하고, 직접 호출은 단일 역할,
  부분 지정은 지정 역할만 활성화한다. 숙의 요청은 요청 대상 역할을 기준으로
  durable barrier를 만들고, Claude가 1차 의견 수집 후 최종 통합한다.
- 신뢰 경계: `agent_message.v1` HMAC 서명·key id·trust domain·hop/round 검증,
  민감정보 차단, cross-process durable dedup, coordination 결과의 서명 검증을
  실제 런타임 경로에 연결했다. LaunchAgent에는 권한 0600 키 파일을 주입했다.
- 단일 소유권: canonical Codex는 `com.multiagent.engine`이며, legacy direct
  Codex bridge는 기본 실행을 거부하고 기존 plist도 disabled 상태로 유지한다.
  Telegram 봇은 무주소 그룹 수신 권한을 시작 시 검증한다.
- 검증: mac-agent `451 tests`, engine-repo `43 tests`, Python compileall,
  terminal shim `bash -n`, LaunchAgent plist lint를 통과했다.
- 독립 재검토 결과: Antigravity `changes_required`, Claude의 최신 완료 재검토도
  `changes_required`였다. 두 리뷰가 공통으로 남긴 중앙 egress queue/backpressure,
  approval·lease provenance 서명, key rotation, 다중 프로세스 end-to-end 및
  실제 Telegram canary 검증은 아직 완료 기준을 충족하지 않는다.

따라서 현재 상태는 핵심 라우팅·숙의·서명 경로를 구현한 **부분 적용 상태**이며,
독립 리뷰 필수 게이트 `pass` 전에는 작업지시서의 최종 완료로 표시하지 않는다.

### 후속 구현 라운드 (2026-08-02)

- `bin/edge_agent_egress_queue.py`: 채팅별 lock, 전역 state lock, Telegram 최소
  전송 간격, bounded backpressure를 구현하고 Claude·Antigravity·Roda·canonical
  Codex 전달 경로에 연결했다. Python async 봇은 queue acquire/release를
  `asyncio.to_thread`로 실행해 이벤트 루프를 막지 않는다.
- `AgentMessageKeyring`: 활성 키 교체와 구 키 overlap 검증을 지원한다. 기존 단일
  키 파일 호환성은 유지하며, keyring 디렉터리를 선택적으로 활성화할 수 있다.
- Telegram claim store는 역할별 DB가 아닌 공유 SQLite DB와 역할별 owner allowlist를
  사용한다. `roda`/`gemma` legacy role alias도 숙의 저장 시 정규화한다.
- 후속 검증: mac-agent `463 tests`, engine-repo `43 tests`, compileall, plist lint
  통과. 네 서비스는 drain-aware 재기동 후 `running`이며, 네 봇의 그룹 intake
  검증 로그가 `full_group_intake=True`로 확인됐다.
- 남은 범위: 실제 Telegram canary, approval/cancel/restart E2E, 중앙 queue의
  Telegram API 429 재현 테스트, claim/lease의 암호학적 provenance 증명은 별도
  후속 라운드로 남긴다.

### 제어면·복구 라운드 (2026-08-02)

- `bin/edge_agent_control_plane.py`를 추가해 chat별 승인·취소·작업 상태를
  atomic JSON과 lock으로 영속화했다. 승인 요청·승인/거부·전역 취소 이벤트는
  HMAC 서명, `key_id`, `source_event_id`를 포함하며, 취소는 비종료 하위 작업까지
  cascade한다.
- Claude·Codex·Antigravity·Roda direct bridge와 canonical Codex engine이 같은
  control-plane을 사용한다. 시작·성공·실패·취소를 기록하고, engine 시작 시
  재시작 후 복구 후보 수를 로그로 진단한다. 취소 요청은 공통 vocabulary로
  처리하며, 기존 legacy Codex LaunchAgent는 계속 비활성 상태로 유지한다.
- Telegram claim은 공유 SQLite에 역할별 owner allowlist를 적용하되, broadcast
  fan-out을 막지 않도록 claim key를 `message_key:role=<role>`로 분리했다. 따라서
  동일 역할의 중복 update는 막고, 같은 사용자 발화에 대한 네 역할의 정상 참여는
  허용한다.
- 검증 결과: mac-agent `475 tests`, engine-repo `44 tests` 모두 통과했고,
  compileall·git diff --check·LaunchAgent plist lint를 통과했다. 네 canonical
  서비스는 drain-aware 재기동 후 `state=running`이며, 그룹 intake 권한 로그는
  모두 정상이다.
- 남은 범위: 실제 Telegram canary, Telegram API 429 통합 재현, claim/lease 자체의
  암호학적 provenance 서명, Claude·Antigravity의 최신 독립 `pass` 재검토는 아직
  완료 기준이 아니다. 따라서 본 작업은 기능 적용 상태이지 최종 승인 상태가
  아니다.
- 최신 Antigravity 재검토 시도는 headless 실행 환경의 `command` 권한이 자동
  거부되어 결과를 생성하지 못했다. 이는 `pass`나 새로운 `changes_required` 판정으로
  해석하지 않으며, 직전 완료 검토의 `changes_required`를 유지한다.

### 잔여 차단항목 즉시 처리 (2026-08-02)

- Antigravity 재검토가 지적한 Telegram 429 처리 공백을 해소했다. canonical engine은
  Telegram API `error_code=429`의 `parameters.retry_after`를 읽어 bounded retry하고,
  direct Claude·Antigravity·Roda bridge는 python-telegram-bot `RetryAfter`를 받아
  동일 chat egress slot을 재획득한 뒤 최대 1회 동적 backoff 재시도한다.
- claim/lease에는 `claim_provenance_events`와 signed session lease metadata를
  추가했다. fencing token, owner, root task, 상태 전이를 HMAC 이벤트로 기록하고
  조회 시 검증한다.
- 전체 검증: mac-agent `475 tests`, engine-repo `44 tests` 모두 통과. 네 서비스
  재기동 후 canonical Codex가 실제 Telegram API canary를 전송했고, delivery
  `canonical-canary-20260802-r2`가 `succeeded`, Telegram `message_id=285`로
  확인됐다.
- 최신 독립 reviewer 결과가 반환되기 전까지 최종 `pass`를 선언하지 않는다.

### 공식 독립 최종 재검토 완료 (2026-08-02)

- Antigravity 공식 독립 reviewer 판정: `pass`
- blocking issues: `none`
- missing requirements: `none`
- reviewer가 mac-agent `475/475`, engine-repo `44/44`, post-restart canary
  `message_id=285`, ingress, signed messages, shared egress, approval/cancel/
  restart control-plane, signed claim/lease provenance, canonical LaunchAgent,
  RetryAfter backoff를 확인했다.
- 남은 것은 기술 차단항목이 아니라 rollback을 위한 legacy disabled plist 보존뿐이다.
- Claude 공식 독립 reviewer도 `pass`, blocking issues `none`을 반환했다. Claude와
  Antigravity 필수 reviewer 게이트가 모두 통과했다.
