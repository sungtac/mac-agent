# knot ingest 병렬화 설계

## 배경

knot은 `$KNOT_VAULT`에 있는 개인 지식 vault이며 공개 저장소다. knot의 네 가지
동작인 save, ingest, query, lint 가운데 이 문서는 ingest만 다룬다.

현재 무인 자동화는 `scripts/drain.sh`가 launchd 또는 cron으로 깨어나 agy, claude,
codex, gemini 중 하나의 러너 CLI를 파일 하나씩 순차 호출하는 구조다. 서브에이전트
팬아웃은 전혀 없다.

개선은 두 단계로 진행한다.

1. `drain.sh`에 이미 있는 버그를 먼저 수정한다.
2. Claude Code Workflow 기반 Map/Reduce 병렬 ingest를 추가한다.

## 검토 이력 요약

최초안은 draft를 knot-vault의 git worktree에서 만들고 git merge로 합치는 구조였다.
Codex와 Antigravity(agy)의 독립 검토에서 다음과 같은 공통 사유로 반려됐다.

- git 충돌이 없다는 사실은 의미적 정합성을 보장하지 않는다. 같은 개념을 서로 다른
  slug로 만들거나 서로 다른 관점으로 덮어써도 git은 충돌로 판단하지 않는다.
- `index.md`와 `log.md`는 병렬 draft에서 사실상 매번 충돌한다.
- worktree가 남거나 정리에 실패할 위험이 있다.
- 1처리=1커밋 불변식이 깨진다.

대안으로 Map은 병렬·읽기 전용으로 구조화된 JSON만 반환하고, Reduce는 직렬로 실제
적용과 커밋을 담당하는 구조를 확정했다. Claude Code Workflow의
`agent(prompt, {schema})`가 이 패턴에 맞으므로 draft 단계의 worktree 격리는 필요하지
않다.

Codex가 실제 `drain.sh`와 `lint.py` 코드를 대조하는 과정에서 병렬화와 무관한 기존
버그도 여러 개 발견했다. 이 버그는 1단계에서 먼저 수정한다.

## 1단계: drain.sh 버그 수정

적용 대상은 knot-vault이며, 지원 근거 파일은 `$KNOT_VAULT/scripts/drain.sh`와
`$KNOT_VAULT/prompts/ingest.md`다. 공개 저장소의 벤더 중립 원칙을 유지한다.

| # | 버그 | 수정 |
|---|---|---|
| 1 | 러너 exit code 무시 — 실패해도 HEAD/inbox만 바뀌면 성공 처리 | `run_ingest` 반환값을 파이프 없이 직접 캡처해 명시적으로 체크한다. 0이 아니면 무조건 실패 처리한다. |
| 2 | "가장 오래된 파일"이라는 의도와 달리 실제 선택은 `ls \| head -1`로 이름순이며 mtime 기준이 아님 | mtime 기준 정렬인 `ls -1rt`로 교체한다. macOS와 Linux 양쪽에서 호환되게 한다. |
| 3 | 복원 로직이 `HEAD_BEFORE`를 캡처하지만 실제로는 `git reset --hard HEAD`, 즉 `HEAD_AFTER`를 사용함. 잘못된 커밋이 이미 HEAD가 된 경우 복원이 사실상 무동작 | `git reset --hard "$HEAD_BEFORE"`와 `git clean -fd -- inbox/ raw/`를 세트로 실행해 clean 범위를 제한하고 untracked 오염을 제거한다. |
| 4 | 동시 실행 방지가 dirty-tree 체크뿐이라 실제 lock이 아니며 race가 있음 | `.knot/drain.lock/`을 `mkdir`로 원자적으로 생성하고 PID, 호스트, 시작 시각, base HEAD를 기록한다. lock 생성 자체가 dirty-tree로 잡히지 않도록 `.knot/`을 `.gitignore`에 명시한다. |
| 5 | lint 실패를 로그만 남겨 drain 전체가 성공 종료할 수 있음 | lint ERROR가 있으면 drain 전체를 실패 처리하고 `HEAD_BEFORE`로 롤백한다. |
| 6 | 진척 판정이 inbox 총개수 감소만 확인함 | 건별로 정확한 inbox 경로 소멸, raw 경로 생성, 두 파일의 SHA-256 동일, 정확히 1커밋, lint 통과를 모두 확인한다. `ingest.md`가 raw 이동 시 내용 수정 금지를 이미 명시하므로 SHA-256 검사는 유효하다. |
| 7 | 병렬 호출자가 다음에 처리할 파일을 지정할 방법이 없음 | `ingest.md`에 선택적 대상 파일 인자를 추가한다. 인자가 없으면 기존의 오래된 파일 자동 선택 동작을 유지한다. 인자로 받은 경로가 `inbox/` 하위 실경로인지 검증해 path traversal을 막는다. |
| - | OS 재부팅, OOM kill, SIGKILL 등으로 stale lock이 남으면 무인 launchd/cron이 영구 정지함 | 경고에 그치지 않고 자가치유한다. 같은 호스트면 `kill -0 $PID`로 생존을 확인하고, 프로세스가 죽었거나 타임아웃 예시인 30분을 초과하면 자동 탈취하고 로그를 남긴다. |
| - | 복원 도중 오류가 나면 lock이 해제되지 않을 수 있음 | `trap`에 등록한 `rmdir .knot/drain.lock`이 오류 경로에서도 반드시 실행되도록 보장한다. |
| - | macOS의 BSD 도구와 Linux의 GNU 도구 사이에 `stat`, `date`, `sed -i` 등의 차이가 있음 | POSIX 호환 명령어를 우선 사용하고 벤더 중립 원칙을 유지한다. |

이 표는 knot-vault의 `prompts/`와 `scripts/`에 적용할 변경 계획이다. 두 경로는 사람
승인이 필요한 영역이다. 변경은 아직 실제로 커밋되지 않았으며, 이 설계 문서를 승인한
뒤 별도 작업에서 사람의 승인을 받아 진행한다.

## 2단계: Map/Reduce 병렬 ingest

2단계는 비공개 저장소 mac-agent에 추가하는 Claude Code 전용 기능이다. knot-vault는
벤더 중립을 유지하며 1단계 정도의 최소 변경만 받는다. `drain.sh`에는 `--parallel`과
같은 Claude 전용 플래그를 넣지 않는다.

새 진입점은 `mac-agent/bin/knot-ingest-parallel`이다. `drain.sh`와 분리된 진입점이며
별도의 launchd 또는 cron 스케줄을 사용한다. 배치당 inbox 파일 3~5개만 동시에
처리한다. Workflow 도구의 최대 동시성 상한인 16에 맡기지 않고 명시적으로 제한한다.

### 아키텍처

```text
mac-agent/bin/knot-ingest-parallel
└── 배치 시작
    ├── .knot/drain.lock/ 한 번 획득
    ├── inbox 스냅샷과 base HEAD 기록
    ├── Map: 최대 3~5개 병렬, vault 읽기 전용
    │   ├── inbox 파일 A → agent(prompt, {schema}) → JSON draft A
    │   ├── inbox 파일 B → agent(prompt, {schema}) → JSON draft B
    │   └── inbox 파일 N → agent(prompt, {schema}) → JSON draft N
    ├── Reduce: JSON draft를 한 건씩 직렬 적용
    │   ├── 파일과 SHA-256 재검증
    │   ├── 최신 entity/concept와 의미적 reconcile
    │   ├── index.md/log.md 갱신과 raw 이동
    │   ├── lint 및 건별 진척 조건 검증
    │   └── 단일 ingest: 커밋 또는 해당 건만 실패 격리
    └── 배치 종료
        ├── lock 해제
        └── STATUS/notify 형식 요약 보고
```

### 배치 시작

1단계에서 추가한 `.knot/drain.lock/`을 배치 전체에 대해 한 번 획득한다. 이어서 inbox
스냅샷과 base HEAD를 기록한다.

### Map 단계

각 inbox 파일마다 `agent(prompt, {schema})` 서브에이전트를 띄운다. 최대 3~5개를
병렬로 실행한다. 서브에이전트는 vault를 읽기 전용으로만 참조하며 파일을 직접 쓰지
않는다. source 페이지 초안, entity/concept 생성·갱신 의도, `index.md`와 `log.md`에
추가할 줄, raw 이동 대상 경로를 구조화된 JSON으로만 반환한다.

스키마의 핵심 필드는 다음과 같다.

```json
{
  "inbox_path": "inbox/example.md",
  "source_sha256": "<sha256>",
  "source_slug": "example",
  "entities": [],
  "concepts": [],
  "touched_pages": [],
  "raw_target": "raw/example.md"
}
```

Map 단계는 파일을 전혀 쓰지 않으므로 worktree 격리를 사용하지 않는다.

### Reduce 단계

Map 결과를 순서대로 받아 한 건씩 직렬 처리한다.

- (a) 처리 직전에 inbox 파일이 여전히 존재하며 스냅샷 시점과 SHA-256이 같은지 다시
  확인한다. Map 실행 중 사람이 inbox를 변경했을 가능성에 대비한다.
- (b) 앞선 draft가 반영됐을 수 있는 최신 entity/concept 상태를 다시 읽고 이번 draft의
  제안과 의미적으로 reconcile한다. 같은 개념을 다른 slug로 만들려는 경우나 기존
  설명과 충돌하는 경우는 git merge가 아니라 여기서 판단한다.
- (d) `index.md`와 `log.md`를 갱신하고 raw로 이동한 뒤 `lint.py`를 실행한다.
- (e) 정확한 inbox 경로 소멸, raw 경로 생성, SHA-256 동일, 정확히 1커밋, lint 통과라는
  건별 진척 판정 기준을 모두 만족하는 단일 `ingest:` 커밋을 만든다.
- (f) 실패한 건만 실패 처리해 inbox에 남기고 다음 건을 계속 처리한다. 재실행해도 같은
  결과를 유지하는 idempotent 동작이다.

배치가 끝나면 lock을 해제한다. 결과는 기존 `drain.sh`의 STATUS/notify 패턴과 같은
형식으로 요약 보고한다.

### 보안

inbox 내용은 prompt injection을 포함할 수 있는 신뢰할 수 없는 외부 입력이다. Map
서브에이전트에는 파일 읽기 정도의 최소 권한만 부여한다. inbox 본문에 담긴 지시문을
명령으로 실행하지 않는다는 규칙을 프롬프트에 명시한다.

공개 저장소 자동 ingest와 `--dangerously-skip-permissions` 같은 권한 우회 실행 옵션을
함께 사용하면 신뢰할 수 없는 입력이 높은 권한과 결합되는 위험이 있다. 이 위험의 해소는
이번 설계 범위 밖이지만 알려진 위험으로 기록한다.

## 테스트 전략

### 1단계

로컬에서 다음 상황을 각각 재현해 의도한 실패, 복원, 자가치유가 동작하는지 확인한다.

- 러너가 실패하도록 흉내 낸 상황
- lint ERROR가 발생하는 상황
- lock이 이미 잡혀 있는 상황
- 죽은 PID가 기록된 stale lock 상황

### 2단계

- inbox에 서로 무관한 파일 3~5개를 넣고 정상적으로 병렬 처리되는지 확인한다.
- 두 inbox 파일이 같은 entity/concept를 언급하도록 충돌 케이스를 만들고 Reduce의
  reconcile이 의미적으로 올바르게 합치는지 확인한다.
- Map 실행 중 서브에이전트 하나가 실패해도 나머지는 정상 처리되고 실패한 건만 inbox에
  남는지 확인한다.

## 롤아웃

구현과 검증은 다음 순서로 진행한다.

1. knot-vault의 1단계 변경 diff를 사람이 직접 승인한 뒤 커밋한다.
2. 1단계의 실패, 복원, lock 동작을 검증한다.
3. mac-agent에 2단계 신규 진입점을 구현한다.
4. Map/Reduce 통합 동작을 검증한다.
5. 필요할 때 별도 launchd 또는 cron 스케줄을 opt-in으로 설정한다.

2단계는 mac-agent의 별도 진입점으로 추가되므로 `drain.sh`의 기존 순차 경로는 그대로
유지된다. 신규 병렬 진입점은 opt-in이며 별도 스케줄을 설정하지 않는 한 동작하지 않는다.
