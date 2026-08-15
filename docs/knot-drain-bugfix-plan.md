# knot drain.sh 안정화 (1단계) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: dispatch each task to a fresh subagent (this environment's Agent tool, or the Workflow tool for a deterministic multi-task pipeline — recommended) or use the executing-plans skill to work through this plan task-by-task in the current session. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** knot-vault의 `scripts/drain.sh`(무인 launchd/cron ingest 자동화)가 동시 실행을 막고, 러너 실패·lint 실패를 정확히 감지하고, 실패 시 안전하게 롤백하도록 기존 버그를 고친다.

**Architecture:** `.knot/drain.lock/`을 mkdir으로 원자적으로 얻는 자가치유 lock 라이브러리를 새로 만들어 drain.sh에 연결한다. drain.sh의 파일 선택을 mtime 기준으로 고치고 그 대상을 `prompts/ingest.md`에 명시적으로 넘긴다(ingest.md는 그 인자를 받도록 확장). 러너 exit code, 롤백 대상(HEAD_BEFORE), lint 실패 처리, 건별 진척 판정(SHA-256 기반)을 각각 고친다. 새 의존성 없이 순수 bash/POSIX 유틸리티 + 기존 git/python3만 사용한다.

**Tech Stack:** bash, git, coreutils, python3(`scripts/lint.py`는 무변경). 테스트는 임시 git 저장소를 만들어 실제 lock/rollback/progress 동작을 검증하는 순수 bash assert 스크립트(`scripts/tests/`)로 작성한다.

## Global Constraints

- 적용 대상 저장소: `$KNOT_VAULT`(로컬 경로 예시: `/Users/edge_ai/knot-vault`) — **공개 저장소**. macOS(BSD)/Linux(GNU) 양쪽에서 동작해야 하며 GNU 전용 옵션(`stat -c`, `date -d` 등)을 쓰지 않는다.
- `prompts/`·`scripts/`는 schema.md에 따라 사람 승인이 필요한 영역이다. 이 계획의 각 태스크는 diff를 만들고 커밋 직전에 사람이 확인할 수 있도록 커밋 메시지에 변경 이유를 명시한다.
- `.knot/`는 이미 `.gitignore`에 등록되어 있다(확인됨) — lock 디렉터리 생성이 dirty-tree로 잡히지 않는다.
- stale lock 타임아웃 기본값은 1800초(30분).
- 기존 `RUNLOG`(`$STATE/drain.log`) 로그 포맷과 STATUS 파일 포맷은 유지한다(다른 도구가 파싱할 수 있으므로 필드를 없애지 않는다).

---

## File Structure

- Create: `scripts/lib/drain-lock.sh` — mkdir 기반 원자적 lock 획득/해제 + 자가치유 stale 판정. drain.sh와 향후 mac-agent의 병렬 진입점이 공유해서 쓸 수 있도록 독립 파일로 분리.
- Modify: `scripts/drain.sh` — lock 연결, 파일 선택(mtime), exit code 체크, 롤백 대상, lint 실패 처리, 진척 판정.
- Modify: `prompts/ingest.md` — 선택적 대상 파일 인자 절 추가.
- Create: `scripts/tests/test-drain-lock.sh` — lock 획득/충돌/자가치유 테스트.
- Create: `scripts/tests/test-drain-rollback.sh` — 러너 실패·lint 실패 시 롤백/진척 판정 테스트.
- Create: `scripts/tests/run-all.sh` — 위 두 테스트를 순서대로 실행하는 러너(향후 테스트 추가 시 이 파일에 등록).

---

### Task 1: `.knot/drain.lock/` 원자적·자가치유 lock 라이브러리

**Files:**
- Create: `scripts/lib/drain-lock.sh`
- Test: `scripts/tests/test-drain-lock.sh`

**Interfaces:**
- Produces:
  - `drain_lock_acquire "<vault_path>"` — 함수. 성공 시 exit code 0, `$vault_path/.knot/drain.lock/{pid,host,start_epoch,base_head}`를 기록하고 `trap`으로 프로세스 종료 시 자동 해제를 등록한다. 실패(다른 살아있는 프로세스가 lock을 쥐고 있음) 시 exit code 1, 아무 것도 만들지 않는다.
  - `drain_lock_release "<lockdir_path>"` — 함수. 해당 lock 디렉터리를 제거한다(존재하지 않아도 에러 없음).
  - env `DRAIN_LOCK_STALE_SEC`(기본 1800) — stale 판정 타임아웃(초).

- [ ] **Step 1: 실패하는 테스트를 먼저 쓴다**

`scripts/tests/test-drain-lock.sh`:

```bash
#!/bin/bash
# scripts/tests/test-drain-lock.sh
set -u
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$SELF_DIR/../lib/drain-lock.sh"
FAIL=0

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" != "$actual" ]; then
    echo "FAIL: $desc (expected='$expected' actual='$actual')"
    FAIL=1
  else
    echo "ok: $desc"
  fi
}

new_vault() {
  local d
  d="$(mktemp -d)"
  git -C "$d" init -q
  git -C "$d" commit -q --allow-empty -m "init"
  echo "$d"
}

# 1) 최초 획득은 성공한다
# 주의: drain_lock_acquire는 성공 시 자신을 호출한 셸에 EXIT trap을 걸어 lock을
# 자동 해제한다. 아래처럼 서브셸 안에서 호출하면 서브셸이 끝나는 순간 trap이 발동해
# lock이 즉시 사라지므로, "lock이 실제로 만들어졌는지"는 서브셸이 끝나기 전에(같은
# 서브셸 안에서) 확인해야 한다. 서브셸 밖에서 디렉터리 존재를 검사하면 항상 FAIL한다.
VAULT1="$(new_vault)"
(
  source "$LIB"
  drain_lock_acquire "$VAULT1"
  echo $? > "$VAULT1/.acquire_rc"
  [ -d "$VAULT1/.knot/drain.lock" ] && echo 1 > "$VAULT1/.lockdir_exists" || echo 0 > "$VAULT1/.lockdir_exists"
)
assert_eq "최초 lock 획득 성공" "0" "$(cat "$VAULT1/.acquire_rc")"
assert_eq "lock 디렉터리 생성됨" "1" "$(cat "$VAULT1/.lockdir_exists")"

# 2) 살아있는 프로세스가 쥔 lock은 두번째 획득이 실패한다(동시 실행 중인 자기 자신의 PID를 기록해 시뮬레이션)
VAULT2="$(new_vault)"
mkdir -p "$VAULT2/.knot/drain.lock"
echo "$$" > "$VAULT2/.knot/drain.lock/pid"     # 현재 테스트 프로세스 자신의 PID = 살아있음
hostname > "$VAULT2/.knot/drain.lock/host"
date +%s > "$VAULT2/.knot/drain.lock/start_epoch"
(
  source "$LIB"
  drain_lock_acquire "$VAULT2"
  echo $? > "$VAULT2/.acquire_rc"
)
assert_eq "살아있는 lock은 획득 실패" "1" "$(cat "$VAULT2/.acquire_rc")"

# 3) 죽은 PID가 기록된 lock은 자가치유로 탈취(획득 성공)한다
VAULT3="$(new_vault)"
mkdir -p "$VAULT3/.knot/drain.lock"
echo "999999999" > "$VAULT3/.knot/drain.lock/pid"   # 존재할 수 없는 PID
hostname > "$VAULT3/.knot/drain.lock/host"
date +%s > "$VAULT3/.knot/drain.lock/start_epoch"
(
  source "$LIB"
  drain_lock_acquire "$VAULT3"
  echo $? > "$VAULT3/.acquire_rc"
)
assert_eq "죽은 PID lock은 자가치유로 획득 성공" "0" "$(cat "$VAULT3/.acquire_rc")"

# 4) 타임아웃(오래된 start_epoch)이 지난 lock도 자가치유로 탈취한다
VAULT4="$(new_vault)"
mkdir -p "$VAULT4/.knot/drain.lock"
echo "999999999" > "$VAULT4/.knot/drain.lock/pid"
hostname > "$VAULT4/.knot/drain.lock/host"
echo "0" > "$VAULT4/.knot/drain.lock/start_epoch"   # epoch 0 = 아주 오래됨
(
  DRAIN_LOCK_STALE_SEC=60
  source "$LIB"
  drain_lock_acquire "$VAULT4"
  echo $? > "$VAULT4/.acquire_rc"
)
assert_eq "타임아웃 초과 lock은 자가치유로 획득 성공" "0" "$(cat "$VAULT4/.acquire_rc")"

# 5) drain_lock_release는 lock 디렉터리를 제거한다
VAULT5="$(new_vault)"
mkdir -p "$VAULT5/.knot/drain.lock"
(
  source "$LIB"
  drain_lock_release "$VAULT5/.knot/drain.lock"
)
assert_eq "release 후 lock 디렉터리 없음" "0" "$([ -d "$VAULT5/.knot/drain.lock" ] && echo 1 || echo 0)"

rm -rf "$VAULT1" "$VAULT2" "$VAULT3" "$VAULT4" "$VAULT5"
exit $FAIL
```

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `bash scripts/tests/test-drain-lock.sh`
Expected: `scripts/lib/drain-lock.sh`가 없으므로 `source: no such file` 오류로 전부 FAIL.

- [ ] **Step 3: 최소 구현 작성**

`scripts/lib/drain-lock.sh`:

```bash
#!/bin/bash
# knot drain 동시성 lock — mkdir 기반 원자적 lock, 자가치유 stale 처리.
# 사용: source 후 drain_lock_acquire "$VAULT"를 호출한다. 성공(0)하면
# trap으로 drain_lock_release가 EXIT/INT/TERM에 자동 등록된다.
# 실패(1)하면 다른 실행이 진행 중이라는 뜻이며 아무 것도 만들지 않는다.
#
# 주의(subshell): trap은 그것이 등록된 셸에만 적용된다. drain_lock_acquire를
# `( ... )` 서브셸이나 파이프라인 안에서 호출하면 그 서브셸이 끝나는 순간
# lock이 풀려버린다. drain.sh 본문처럼 최상위 셸에서 source 후 직접 호출할 것 —
# 테스트 코드에서 검증 목적으로 서브셸에 감싸 호출하는 것은 그 서브셸 안에서
# lock 수명이 끝나는 게 의도이므로 문제가 아니다.

DRAIN_LOCK_STALE_SEC="${DRAIN_LOCK_STALE_SEC:-1800}"

drain_lock_release() {
  local lockdir="$1"
  [ -n "$lockdir" ] && [ -d "$lockdir" ] && rm -rf "$lockdir"
}

drain_lock_acquire() {
  local vault="$1"
  local lockdir="$vault/.knot/drain.lock"
  mkdir -p "$vault/.knot" 2>/dev/null

  if ! mkdir "$lockdir" 2>/dev/null; then
    local lock_pid lock_host lock_time now age stale
    lock_pid="$(cat "$lockdir/pid" 2>/dev/null || echo "")"
    lock_host="$(cat "$lockdir/host" 2>/dev/null || echo "")"
    lock_time="$(cat "$lockdir/start_epoch" 2>/dev/null || echo 0)"
    now="$(date +%s)"
    age=$((now - lock_time))
    stale=0

    if [ "$lock_host" = "$(hostname)" ] && [ -n "$lock_pid" ] && ! kill -0 "$lock_pid" 2>/dev/null; then
      stale=1
    fi
    if [ "$age" -gt "$DRAIN_LOCK_STALE_SEC" ]; then
      stale=1
    fi

    if [ "$stale" -eq 1 ]; then
      echo "stale lock 탈취: pid=$lock_pid host=$lock_host age=${age}s" >&2
      rm -rf "$lockdir"
      mkdir "$lockdir" 2>/dev/null || return 1
    else
      return 1
    fi
  fi

  echo "$$" > "$lockdir/pid"
  hostname > "$lockdir/host"
  date +%s > "$lockdir/start_epoch"
  git -C "$vault" rev-parse HEAD > "$lockdir/base_head" 2>/dev/null || echo "none" > "$lockdir/base_head"

  # shellcheck disable=SC2064
  trap "drain_lock_release '$lockdir'" EXIT INT TERM
  return 0
}
```

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `bash scripts/tests/test-drain-lock.sh`
Expected: 5개 assert 모두 `ok:`로 시작, exit code 0.

- [ ] **Step 5: 커밋**

```bash
cd "$KNOT_VAULT"
chmod +x scripts/lib/drain-lock.sh scripts/tests/test-drain-lock.sh
git add scripts/lib/drain-lock.sh scripts/tests/test-drain-lock.sh
git commit -m "feat(drain): mkdir 기반 원자적 lock + 자가치유 stale 처리 추가"
```

---

### Task 2: drain.sh에 lock 연결

**Files:**
- Modify: `scripts/drain.sh:41` (vault 위치 확정 직후, `STATE="$VAULT/.knot"` 앞)
- Modify: `scripts/drain.sh` 끝부분(148행 부근, `exit 0`/`exit 1` 이전) — lock은 Task 1의 trap이 자동 해제하므로 별도 해제 코드는 필요 없다. 여기서는 lock 획득 실패 시의 조기 종료 경로만 추가한다.
- Test: `scripts/tests/test-drain-lock.sh`에 시나리오 추가(아래)

**Interfaces:**
- Consumes: Task 1의 `drain_lock_acquire`, `DRAIN_LOCK_STALE_SEC`.
- Produces: drain.sh가 lock을 못 얻으면 `STATUS` 파일에 "다른 drain 실행 중"을 기록하고 exit code 1로 조용히 중단한다(기존 dirty-tree 가드는 그대로 유지 — lock은 배치 전체, dirty-tree 체크는 매 iteration의 2차 방어선).

- [ ] **Step 1: 실패하는 테스트를 먼저 쓴다**

`scripts/tests/test-drain-lock.sh` 끝(`rm -rf "$VAULT1" ...` 줄 앞)에 추가:

```bash
# 6) drain.sh는 lock을 못 얻으면 즉시 실패 종료한다
VAULT6="$(new_vault)"
mkdir -p "$VAULT6/.knot/drain.lock" "$VAULT6/inbox" "$VAULT6/scripts" "$VAULT6/scripts/lib"
cp "$SELF_DIR/../lib/drain-lock.sh" "$VAULT6/scripts/lib/drain-lock.sh"
cp "$SELF_DIR/../drain.sh" "$VAULT6/scripts/drain.sh"
echo "$$" > "$VAULT6/.knot/drain.lock/pid"      # 현재 프로세스 = 살아있는 lock
hostname > "$VAULT6/.knot/drain.lock/host"
date +%s > "$VAULT6/.knot/drain.lock/start_epoch"
KNOT_VAULT="$VAULT6" bash "$VAULT6/scripts/drain.sh" >/dev/null 2>&1
assert_eq "lock 못 얻으면 drain.sh exit 1" "1" "$?"
assert_eq "lock 못 얻어도 inbox는 그대로" "0" "$(ls "$VAULT6/inbox" | wc -l | tr -d ' ')"
```

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `bash scripts/tests/test-drain-lock.sh`
Expected: 새 assert 2개가 FAIL(drain.sh가 아직 lock을 안 씀 — inbox가 비어 있으니 원래도 바로 종료하지만, 지금은 lock 충돌과 무관하게 "할 일 없음"으로 exit 0을 낼 것이므로 첫 assert가 FAIL).

- [ ] **Step 3: drain.sh에 lock 연결**

`scripts/drain.sh:41` 부근(`cd "$VAULT" || exit 1` 다음 줄)에 삽입:

```bash
# --- 배치 전체 lock (동시 실행 방지) ---
# shellcheck source=lib/drain-lock.sh
source "$VAULT/scripts/lib/drain-lock.sh"
if ! drain_lock_acquire "$VAULT"; then
  echo "다른 drain 실행이 진행 중입니다(lock 획득 실패)" >> "$VAULT/.knot/drain.log" 2>/dev/null
  exit 1
fi
```

**참고:** `drain_lock_acquire`가 성공하면 내부에서 `trap ... EXIT INT TERM`을 이미 등록하므로, drain.sh 끝에서 별도로 lock을 해제하는 코드를 추가하지 않는다(추가하면 이중 trap 등록이 되어 순서가 꼬인다).

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `bash scripts/tests/test-drain-lock.sh`
Expected: 7개 assert 모두 `ok:`, exit code 0.

- [ ] **Step 5: 커밋**

```bash
cd "$KNOT_VAULT"
git add scripts/drain.sh scripts/tests/test-drain-lock.sh
git commit -m "feat(drain): drain.sh 시작 시 배치 lock 획득하도록 연결"
```

---

### Task 3: 대상 파일 mtime 선택 + `ingest.md` 명시적 인자 지원

**Files:**
- Modify: `scripts/drain.sh:93` (`NEXT=$(ls inbox/ | head -1)`)
- Modify: `scripts/drain.sh:47` (`PROMPT="schema.md와 prompts/ingest.md를 정독하고 그대로 실행하라"`)
- Modify: `prompts/ingest.md:5` (2번 항목)
- Test: `scripts/tests/test-drain-rollback.sh`(새로 생성, Task 4에서도 재사용)

**Interfaces:**
- Produces: drain.sh가 매 iteration마다 `inbox/`에서 **mtime이 가장 오래된 파일**을 골라 변수 `NEXT`에 담고, 프롬프트에 `대상 파일: inbox/<파일명>` 문구로 명시적으로 넘긴다. `ingest.md`는 "인자로 대상 파일이 주어지면 그것을 쓰고, 없으면 기존처럼 자동 선택한다"는 절을 추가한다(기존 자동 선택 동작은 그대로 유지 — 사람이 직접 러너를 돌릴 때는 인자가 없다).

- [ ] **Step 1: 실패하는 테스트를 먼저 쓴다**

`scripts/tests/test-drain-rollback.sh`(새 파일, 이 태스크에서는 파일 선택 부분만 검증):

```bash
#!/bin/bash
# scripts/tests/test-drain-rollback.sh
set -u
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAIL=0

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" != "$actual" ]; then
    echo "FAIL: $desc (expected='$expected' actual='$actual')"
    FAIL=1
  else
    echo "ok: $desc"
  fi
}

new_vault() {
  local d
  d="$(mktemp -d)"
  git -C "$d" init -q
  mkdir -p "$d/inbox" "$d/raw" "$d/wiki" "$d/scripts/lib"
  echo "# index" > "$d/index.md"
  echo "# log" > "$d/log.md"
  cp "$SELF_DIR/../lib/drain-lock.sh" "$d/scripts/lib/drain-lock.sh"
  cp "$SELF_DIR/../drain.sh" "$d/scripts/drain.sh"
  git -C "$d" add -A
  git -C "$d" commit -q -m "init"
  echo "$d"
}

# mtime이 다른 두 inbox 파일 중, 이름순으로는 나중이지만 mtime은 더 오래된 파일이 선택되어야 함
VAULT="$(new_vault)"
echo "b" > "$VAULT/inbox/b-newer.md"
sleep 1
echo "a" > "$VAULT/inbox/a-older-by-name-but-newer-mtime.md"
# 위 두 줄은 이름순 정렬 시 a가 먼저 오지만, mtime은 b가 더 오래됨(먼저 생성됨) —
# 즉 "이름순"과 "mtime순"이 서로 다른 결과를 내도록 의도적으로 구성.
touch -t "$(date -v-1H +%Y%m%d%H%M 2>/dev/null || date -d '-1 hour' +%Y%m%d%H%M)" "$VAULT/inbox/b-newer.md" 2>/dev/null

# 러너를 스텁으로 대체: 넘겨받은 프롬프트를 파일에 그대로 적어서 검사
STUB_LOG="$VAULT/.stub-prompt.txt"
cat > "$VAULT/.stub-runner" <<STUB
#!/bin/bash
for a in "\$@"; do
  if [ "\$a" != "-p" ]; then echo "\$a" >> "$STUB_LOG"; fi
done
exit 0
STUB
chmod +x "$VAULT/.stub-runner"

KNOT_VAULT="$VAULT" KNOT_RUNNER="$VAULT/.stub-runner" KNOT_MAX_ITER=1 \
  bash "$VAULT/scripts/drain.sh" >/dev/null 2>&1

assert_eq "mtime이 더 오래된 b-newer.md가 대상으로 선택됨" "1" \
  "$(grep -c "대상 파일: inbox/b-newer.md" "$STUB_LOG" 2>/dev/null || echo 0)"

rm -rf "$VAULT"
exit $FAIL
```

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `bash scripts/tests/test-drain-rollback.sh`
Expected: FAIL(현재 `NEXT`는 이름순 `ls`로 뽑고, 프롬프트에 "대상 파일:" 문구 자체가 없음).

- [ ] **Step 3: drain.sh와 ingest.md 수정**

`scripts/drain.sh:47`을 다음으로 교체:

```bash
PROMPT_BASE="schema.md와 prompts/ingest.md를 정독하고 그대로 실행하라"
```

`scripts/drain.sh:93`(`NEXT=$(ls inbox/ | head -1)`)을 포함한 for 루프 본문 중 관련 부분을 다음으로 교체:

```bash
  NEXT=$(ls -1rt inbox/ 2>/dev/null | head -1)
  PROMPT="$PROMPT_BASE. 대상 파일: inbox/$NEXT"
  echo "[$(date '+%F %T')] [$i] 처리: $NEXT (잔여 $COUNT)" >> "$RUNLOG"
  run_ingest >> "$RUNLOG" 2>&1
```

(`run_ingest` 함수 내부의 `"$PROMPT"` 참조는 그대로 둔다 — 루프에서 매 iteration마다 `PROMPT`를 갱신하므로 함수는 최신 값을 그대로 쓴다.)

`prompts/ingest.md:5`(2번 항목)를 다음으로 교체:

```markdown
2. 대상 파일을 정한다: 호출 프롬프트에 "대상 파일: inbox/<이름>"이 명시되어 있으면 그 파일을 쓴다.
   명시가 없으면 `inbox/`에서 가장 오래된 파일 1건을 고른다(기본 1건, 명시적 지시가 있을 때만
   최대 3건). 대상이 없거나 `inbox/`가 비어 있으면 "할 일 없음"으로 종료한다.
   - 텍스트(md·txt)가 벤더중립 기본. PDF·이미지 등 rich 포맷은 읽을 수 없으면 건너뛰고 보고한다.
```

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `bash scripts/tests/test-drain-rollback.sh`
Expected: `ok:`, exit code 0.

- [ ] **Step 5: 커밋**

```bash
cd "$KNOT_VAULT"
chmod +x scripts/tests/test-drain-rollback.sh
git add scripts/drain.sh prompts/ingest.md scripts/tests/test-drain-rollback.sh
git commit -m "fix(drain): mtime 기준 파일 선택 + 대상 파일 명시적 전달 지원"
```

---

### Task 4: 러너 exit code 존중 + 롤백 대상(HEAD_BEFORE) + lint 실패 게이트

**Files:**
- Modify: `scripts/drain.sh:95-106`(for 루프 본문의 진척 판정/롤백 부분)
- Modify: `scripts/drain.sh:112-116`(lint 실행 부분)
- Test: `scripts/tests/test-drain-rollback.sh`(시나리오 추가)

**Interfaces:**
- Consumes: Task 3에서 만든 `.stub-runner` 패턴(테스트에서 재사용), `NEXT`/`PROMPT` 변수.
- Produces: `run_ingest`의 실제 exit code를 `RC_INGEST` 변수에 담아 판정에 쓴다. 항목 단위 실패 시 `git reset --hard "$HEAD_BEFORE"`로 그 항목만 복원한다(기존 `git reset --hard HEAD`의 `HEAD`를 `HEAD_BEFORE`로 교체). `BATCH_HEAD_BEFORE`(루프 시작 전 HEAD)를 새로 도입한다 — lint는 배치 전체를 한 번만 검사하므로 실패 원인이 어느 항목인지 특정할 수 없다. 따라서 lint ERROR가 있으면 마지막 항목(`HEAD_BEFORE`)이 아니라 **배치 전체**(`BATCH_HEAD_BEFORE`)로 되돌리고 `DONE`을 0으로 재설정한다.

- [ ] **Step 1: 실패하는 테스트를 먼저 쓴다**

`scripts/tests/test-drain-rollback.sh`의 `rm -rf "$VAULT"` 줄 앞에 추가:

```bash
# 러너가 실패(exit 1)하면서도 커밋을 만들어버린 상황 — HEAD_BEFORE로 복원되어야 함
VAULT2="$(new_vault)"
echo "content" > "$VAULT2/inbox/only.md"

cat > "$VAULT2/.stub-runner" <<'STUB'
#!/bin/bash
cd "$KNOT_VAULT" || exit 1
mv inbox/only.md raw/only.md
git add -A
git commit -q -m "ingest: only (broken)"
exit 1
STUB
chmod +x "$VAULT2/.stub-runner"

BEFORE_HEAD="$(git -C "$VAULT2" rev-parse HEAD)"
KNOT_VAULT="$VAULT2" KNOT_RUNNER="$VAULT2/.stub-runner" KNOT_MAX_ITER=1 \
  bash "$VAULT2/scripts/drain.sh" >/dev/null 2>&1
AFTER_HEAD="$(git -C "$VAULT2" rev-parse HEAD)"

assert_eq "러너 실패 시 HEAD가 원복됨" "$BEFORE_HEAD" "$AFTER_HEAD"
assert_eq "러너 실패 시 inbox 파일이 되돌아옴" "1" \
  "$(ls "$VAULT2/inbox" 2>/dev/null | wc -l | tr -d ' ')"

rm -rf "$VAULT2"

# lint ERROR가 나면 drain 전체가 실패 처리되고 HEAD가 원복되어야 함
VAULT3="$(new_vault)"
echo "content" > "$VAULT3/inbox/bad.md"
cat > "$VAULT3/scripts/lint.py" <<'PYEOF'
import sys
print("ERROR: 의도된 테스트 오류")
sys.exit(1)
PYEOF

cat > "$VAULT3/.stub-runner" <<'STUB'
#!/bin/bash
cd "$KNOT_VAULT" || exit 1
mv inbox/bad.md raw/bad.md
git add -A
git commit -q -m "ingest: bad"
exit 0
STUB
chmod +x "$VAULT3/.stub-runner"

BEFORE_HEAD3="$(git -C "$VAULT3" rev-parse HEAD)"
KNOT_VAULT="$VAULT3" KNOT_RUNNER="$VAULT3/.stub-runner" KNOT_MAX_ITER=1 \
  bash "$VAULT3/scripts/drain.sh" >/dev/null 2>&1
RC3=$?
AFTER_HEAD3="$(git -C "$VAULT3" rev-parse HEAD)"

assert_eq "lint ERROR면 drain이 실패 exit code" "1" "$RC3"
assert_eq "lint ERROR면 HEAD가 원복됨" "$BEFORE_HEAD3" "$AFTER_HEAD3"

rm -rf "$VAULT3"

# 배치에 파일이 2개 있고 둘 다 성공적으로 처리됐지만, 그 다음 lint가 실패하는 상황
# → 마지막 항목뿐 아니라 이번 배치에서 만든 커밋 2개가 전부 되돌아가야 한다
# (HEAD_BEFORE만 쓰면 마지막 1개만 되돌고 첫 번째 성공 커밋이 남는 회귀가 생김)
VAULT3B="$(new_vault)"
echo "first" > "$VAULT3B/inbox/1-first.md"
echo "second" > "$VAULT3B/inbox/2-second.md"
cat > "$VAULT3B/scripts/lint.py" <<'INNERLINT'
import sys
print("ERROR: 의도된 테스트 오류")
sys.exit(1)
INNERLINT

cat > "$VAULT3B/.stub-runner" <<'INNERSTUB'
#!/bin/bash
cd "$KNOT_VAULT" || exit 1
NEXT_ARG=""
for a in "$@"; do
  case "$a" in
    *"대상 파일: inbox/"*) NEXT_ARG="${a##*inbox/}" ;;
  esac
done
mv "inbox/$NEXT_ARG" "raw/$NEXT_ARG"
git add -A
git commit -q -m "ingest: $NEXT_ARG"
exit 0
INNERSTUB
chmod +x "$VAULT3B/.stub-runner"

BEFORE_HEAD3B="$(git -C "$VAULT3B" rev-parse HEAD)"
KNOT_VAULT="$VAULT3B" KNOT_RUNNER="$VAULT3B/.stub-runner" KNOT_MAX_ITER=5 \
  bash "$VAULT3B/scripts/drain.sh" >/dev/null 2>&1
AFTER_HEAD3B="$(git -C "$VAULT3B" rev-parse HEAD)"

assert_eq "2건 성공 후 lint 실패 시 HEAD가 배치 시작 시점으로 원복됨" "$BEFORE_HEAD3B" "$AFTER_HEAD3B"
assert_eq "2건 성공 후 lint 실패 시 두 inbox 파일 모두 되돌아옴" "2" \
  "$(ls "$VAULT3B/inbox" 2>/dev/null | wc -l | tr -d ' ')"

rm -rf "$VAULT3B"
```

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `bash scripts/tests/test-drain-rollback.sh`
Expected: 새 4개 assert 모두 FAIL(현재 `git reset --hard HEAD`는 이미 잘못된 커밋이 HEAD가 된 뒤라 무동작이고, lint ERROR는 로그만 남기고 exit 0으로 끝남).

- [ ] **Step 3: drain.sh 수정**

`scripts/drain.sh:85-107`(for 루프 전체)을 다음으로 교체:

```bash
BATCH_HEAD_BEFORE=$(git rev-parse HEAD 2>/dev/null || echo none)  # 배치 전체 롤백 기준점(lint 실패 시 사용)
for ((i=1; i<=MAX_ITER; i++)); do
  COUNT=$(ls inbox/ 2>/dev/null | wc -l | tr -d ' ')
  [ "$COUNT" -eq 0 ] && break
  if [ -n "$(git status --porcelain)" ]; then
    STOP_REASON="시작 가드 실패: 트리 더티(외부 변경 가능성, reset 안 함)"
    break
  fi
  HEAD_BEFORE=$(git rev-parse HEAD 2>/dev/null || echo none)
  NEXT=$(ls -1rt inbox/ 2>/dev/null | head -1)
  PROMPT="$PROMPT_BASE. 대상 파일: inbox/$NEXT"
  echo "[$(date '+%F %T')] [$i] 처리: $NEXT (잔여 $COUNT)" >> "$RUNLOG"
  run_ingest >> "$RUNLOG" 2>&1
  RC_INGEST=$?
  NEWCOUNT=$(ls inbox/ 2>/dev/null | wc -l | tr -d ' ')
  if [ "$RC_INGEST" -ne 0 ] || [ "$NEWCOUNT" -ge "$COUNT" ]; then
    git reset --hard "$HEAD_BEFORE" >> "$RUNLOG" 2>&1
    git clean -fd -- inbox/ raw/ >> "$RUNLOG" 2>&1
    STOP_REASON="진척 없음($NEXT: 러너 exit=$RC_INGEST, 새 커밋/inbox 감소 미충족). $HEAD_BEFORE로 복원"
    break
  fi
  DONE=$((DONE+1))
  echo "[$(date '+%F %T')] [$i] 완료: $NEXT → $(git log --oneline -1)" >> "$RUNLOG"
  sleep 5
done
```

`scripts/drain.sh:112-116`(lint 실행 부분)을 다음으로 교체:

```bash
LINT_LINE=""
LINT_FAILED=0
if [ -z "$(git status --porcelain)" ] && [ -f scripts/lint.py ]; then
  if python3 scripts/lint.py >> "$RUNLOG" 2>&1; then
    LINT_LINE="lint OK"
  else
    LINT_LINE="lint ERROR(상세 .knot/drain.log)"
    LINT_FAILED=1
  fi
fi
if [ "$LINT_FAILED" -eq 1 ]; then
  # lint는 루프가 끝난 뒤 vault 전체 상태를 검사하므로, 실패 원인이 이번 배치의 어느
  # 항목이었는지 알 수 없다. 마지막 항목만(HEAD_BEFORE) 되돌리면 그 이전에 처리한
  # 항목들의 커밋이 lint 실패 상태로 남는다 — 배치 시작 시점(BATCH_HEAD_BEFORE)까지
  # 전부 되돌려 이번 실행에서 만든 커밋을 모두 취소한다(idempotent: 다음 실행에서 재시도).
  git reset --hard "$BATCH_HEAD_BEFORE" >> "$RUNLOG" 2>&1
  git clean -fd -- inbox/ raw/ >> "$RUNLOG" 2>&1
  STOP_REASON="${STOP_REASON:+$STOP_REASON; }lint ERROR로 인해 배치 전체를 $BATCH_HEAD_BEFORE로 복원"
  DONE=0  # 이번 배치의 성공 커밋도 전부 되돌아갔으므로 카운트를 무효화한다
fi
```

drain.sh 맨 끝(146행 부근)의 exit 판정을 다음으로 교체:

```bash
if [ "$DONE" -eq 0 ] && [ "$REMAIN" -gt 0 ]; then
  exit 1
fi
if [ "$LINT_FAILED" -eq 1 ]; then
  exit 1
fi
exit 0
```

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `bash scripts/tests/test-drain-rollback.sh`
Expected: 모든 assert `ok:`, exit code 0.

- [ ] **Step 5: 커밋**

```bash
cd "$KNOT_VAULT"
git add scripts/drain.sh scripts/tests/test-drain-rollback.sh
git commit -m "fix(drain): 러너 exit code 존중, HEAD_BEFORE 롤백, lint 실패 게이트"
```

---

### Task 5: 건별 진척 판정(SHA-256 기반)으로 교체

**Files:**
- Modify: `scripts/drain.sh`(Task 4에서 만든 for 루프 진척 판정 블록)
- Test: `scripts/tests/test-drain-rollback.sh`(시나리오 추가)

**Interfaces:**
- Consumes: Task 4의 `HEAD_BEFORE`, `NEXT`, `RC_INGEST`.
- Produces: 진척 판정 함수 `drain_progress_ok "<inbox_path_before>" "<vault>"`(drain.sh 내부 로컬 함수) — 다음을 모두 만족해야 성공으로 본다: (1) `inbox/$NEXT`가 더 이상 없음, (2) `raw/`에 `$NEXT`와 파일명이 일치하는(날짜 접두어 제외) 새 파일이 있고 그 SHA-256이 처리 전 `inbox/$NEXT`의 SHA-256과 같음, (3) `git rev-parse HEAD`가 `HEAD_BEFORE`와 다르면서 `git rev-list --count "$HEAD_BEFORE..HEAD"`가 정확히 1.

- [ ] **Step 1: 실패하는 테스트를 먼저 쓴다**

`scripts/tests/test-drain-rollback.sh`의 `rm -rf "$VAULT3"` 줄 앞에 추가:

```bash
# 러너가 exit 0을 반환하고 inbox도 줄였지만, raw로 옮긴 파일 내용이 원본과 달라진(정책 위반) 상황
# → 기존 판정(exit code+inbox 감소)은 성공으로 오인하지만, SHA-256 기반 판정은 실패로 잡아야 함
VAULT4="$(new_vault)"
echo "original content" > "$VAULT4/inbox/tampered.md"

cat > "$VAULT4/.stub-runner" <<'STUB'
#!/bin/bash
cd "$KNOT_VAULT" || exit 1
echo "tampered content" > raw/tampered.md   # 원본과 다른 내용으로 raw에 씀(정책 위반 시뮬레이션)
rm inbox/tampered.md
git add -A
git commit -q -m "ingest: tampered"
exit 0
STUB
chmod +x "$VAULT4/.stub-runner"

BEFORE_HEAD4="$(git -C "$VAULT4" rev-parse HEAD)"
KNOT_VAULT="$VAULT4" KNOT_RUNNER="$VAULT4/.stub-runner" KNOT_MAX_ITER=1 \
  bash "$VAULT4/scripts/drain.sh" >/dev/null 2>&1
RC4=$?
AFTER_HEAD4="$(git -C "$VAULT4" rev-parse HEAD)"

assert_eq "내용이 변조된 raw 이동은 실패로 판정됨" "$BEFORE_HEAD4" "$AFTER_HEAD4"
assert_eq "drain 전체도 실패 exit code" "1" "$RC4"

rm -rf "$VAULT4"
```

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `bash scripts/tests/test-drain-rollback.sh`
Expected: 새 2개 assert FAIL(현재는 exit 0 + inbox 감소만 보므로 성공으로 오인하고 커밋이 그대로 남음).

- [ ] **Step 3: drain.sh 진척 판정 로직 교체**

`scripts/drain.sh`에서 `PROMPT_BASE=...` 줄 위에 헬퍼 함수를 추가:

```bash
# macOS(shasum)와 Linux(sha256sum) 양쪽에서 동작하는 sha256 헬퍼.
_sha256() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'
  else
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
  fi
}

# 건별 진척 판정 — exit code만으로는 부족하므로 실제 결과를 재검증한다.
# 인자: 1=inbox 파일명(NEXT) 2=처리 전 inbox 파일의 sha256 3=HEAD_BEFORE
drain_progress_ok() {
  local next="$1" sha_before="$2" head_before="$3"

  [ -e "inbox/$next" ] && return 1  # 여전히 inbox에 남아있으면 실패

  # raw로 이동할 때 ingest.md 규약(YYYY-MM-DD-<원래이름>)에 따라 날짜 접두어가 붙으므로
  # 파일명 끝이 "-$next"로 끝나는 파일을 찾는다. 접두어 없이 그대로 옮겨진 경우도 대비한다.
  local raw_match
  raw_match=$(find raw -maxdepth 1 -type f -name "*-$next" 2>/dev/null | head -1)
  [ -z "$raw_match" ] && raw_match=$(find raw -maxdepth 1 -type f -name "$next" 2>/dev/null | head -1)
  [ -z "$raw_match" ] && return 1

  local sha_after
  sha_after=$(_sha256 "$raw_match")
  [ "$sha_before" != "$sha_after" ] && return 1

  local head_after commit_count
  head_after=$(git rev-parse HEAD 2>/dev/null || echo none)
  [ "$head_after" = "$head_before" ] && return 1
  commit_count=$(git rev-list --count "$head_before..$head_after" 2>/dev/null || echo 0)
  [ "$commit_count" != "1" ] && return 1

  return 0
}
```

for 루프 본문(Task 4에서 만든 버전)의 진척 판정 부분을 다음으로 교체:

```bash
  HEAD_BEFORE=$(git rev-parse HEAD 2>/dev/null || echo none)
  NEXT=$(ls -1rt inbox/ 2>/dev/null | head -1)
  SHA_BEFORE=$(_sha256 "inbox/$NEXT")
  PROMPT="$PROMPT_BASE. 대상 파일: inbox/$NEXT"
  echo "[$(date '+%F %T')] [$i] 처리: $NEXT (잔여 $COUNT)" >> "$RUNLOG"
  run_ingest >> "$RUNLOG" 2>&1
  RC_INGEST=$?
  if [ "$RC_INGEST" -ne 0 ] || ! drain_progress_ok "$NEXT" "$SHA_BEFORE" "$HEAD_BEFORE"; then
    git reset --hard "$HEAD_BEFORE" >> "$RUNLOG" 2>&1
    git clean -fd -- inbox/ raw/ >> "$RUNLOG" 2>&1
    STOP_REASON="진척 판정 실패($NEXT: 러너 exit=$RC_INGEST 또는 SHA-256/커밋 불일치). $HEAD_BEFORE로 복원"
    break
  fi
  DONE=$((DONE+1))
  echo "[$(date '+%F %T')] [$i] 완료: $NEXT → $(git log --oneline -1)" >> "$RUNLOG"
  sleep 5
```

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `bash scripts/tests/test-drain-rollback.sh`
Expected: 모든 assert `ok:`, exit code 0.

- [ ] **Step 5: 회귀 확인 — 전체 테스트 스위트**

`scripts/tests/run-all.sh` 생성:

```bash
#!/bin/bash
set -u
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAIL=0
for t in "$SELF_DIR"/test-*.sh; do
  echo "=== $t ==="
  bash "$t" || FAIL=1
done
exit $FAIL
```

Run: `chmod +x scripts/tests/run-all.sh && bash scripts/tests/run-all.sh`
Expected: Task 1~5에서 작성한 모든 테스트가 `ok:`로 통과, exit code 0.

- [ ] **Step 6: 커밋**

```bash
cd "$KNOT_VAULT"
chmod +x scripts/tests/run-all.sh
git add scripts/drain.sh scripts/tests/test-drain-rollback.sh scripts/tests/run-all.sh
git commit -m "fix(drain): SHA-256 기반 건별 진척 판정으로 교체"
```

---

## 알려진 한계 (이번 1단계 범위 밖)

- **파일명 공백/특수문자**: `ls -1rt`, `for a in "$@"` 등 이 계획의 코드는 공백이나 개행이 든
  inbox 파일명까지 안전하게 다루지 않는다. 기존 drain.sh도 같은 한계가 있었다(새로 만든 문제가
  아님). knot vault의 파일명은 사람이 직접 짓는 슬러그 규칙을 따르므로 실무 위험은 낮지만,
  완전한 NUL-safe 처리(`find -print0`/`read -d ''` 등으로 전면 재작성)는 범위가 커서 이번
  1단계에는 포함하지 않는다. 필요해지면 별도 계획으로 다룬다.
- **Reduce 단계와의 조합은 2단계(mac-agent Map/Reduce)에서 다룬다**: 이 계획은 knot-vault의
  `drain.sh` 단독 동작만 고친다. `BATCH_HEAD_BEFORE`/`drain_progress_ok`/`_sha256` 등은 2단계
  설계 문서(`docs/knot-ingest-parallelization.md`)가 참조하는 인터페이스이므로, 2단계 계획을
  작성할 때 이름과 동작이 이 계획과 일치하는지 다시 확인한다.

## Self-Review 체크리스트 (실행자 참고용)

- [ ] 설계 문서(`mac-agent/docs/knot-ingest-parallelization.md`) 1단계 표의 7개 버그 + stale lock/trap 순서/BSD·GNU 호환 3개 항목이 모두 Task 1~5 어딘가에서 다뤄지는지 확인.
- [ ] 새 함수(`drain_lock_acquire`, `drain_lock_release`, `drain_progress_ok`)의 이름·인자 순서가 정의한 Task와 사용하는 Task 사이에서 일치하는지 확인.
- [ ] `git clean -fd -- inbox/ raw/`가 `index.md`/`log.md`(추적 파일)는 건드리지 않음을 확인(clean은 untracked만 지움 — tracked 파일 복원은 `git reset --hard`가 담당).
- [ ] 이 계획은 knot-vault(공개 저장소)만 변경한다. mac-agent의 2단계(Map/Reduce)는 별도 계획으로 뒤에 작성한다.
- [ ] lint 실패 롤백이 `BATCH_HEAD_BEFORE`(배치 전체)를 쓰는지, 마지막 항목의 `HEAD_BEFORE`로 잘못 좁혀지지 않았는지 확인(다건 배치에서 앞선 성공 커밋이 남는 회귀 재발 방지).
