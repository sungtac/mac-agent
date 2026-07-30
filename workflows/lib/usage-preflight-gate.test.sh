#!/usr/bin/env bash
# usage-preflight-gate.test.sh
#
# 2026-07-30 버그(5h는 멀쩡한데 7d가 빠듯해서 만들어진 level=red 때문에
# 엉뚱하게 SKIP되던 문제)의 재발 방지 테스트.
#
# 판정 로직(usage-preflight-gate.py)에 고정된 fixture JSON을 stdin으로 직접
# 먹여서 검증한다 — 실제 `coach` 명령을 스텁으로 바꿔치기하려 했으나,
# usage-preflight-gate.sh가 `export PATH="/opt/homebrew/bin:...:$PATH"`로
# 항상 실제 coach가 설치된 경로를 PATH 맨 앞에 두기 때문에 얕은 PATH 트릭으론
# 진짜 coach를 못 가린다(실제 usage량이 매일 바뀌어 테스트가 들쭉날쭉해지는
# 걸 막으려던 원래 목적과 상충). 그래서 판정 로직을 usage-preflight-gate.py로
# 분리해 fixture로 직접 찌른다. 셸 래퍼(coach 호출 + PATH + fail-open 처리)
# 자체는 마지막에 실제 coach로 한 번 더 살아있는지만 확인한다(값은 매일
# 바뀌므로 assert하지 않고 출력만 보여줌).
#
# 실행: bash workflows/lib/usage-preflight-gate.test.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE_PY="$SCRIPT_DIR/usage-preflight-gate.py"
GATE_SH="$SCRIPT_DIR/usage-preflight-gate.sh"

FAIL=0
run_case() {
  local json="$1" actor="$2" floor="${3:-10}"
  printf '%s' "$json" | python3 "$GATE_PY" "$actor" "$floor"
}

check() {
  local desc="$1" actual="$2" expect_substr="$3"
  if [[ "$actual" == *"$expect_substr"* ]]; then
    echo "PASS: $desc"
  else
    echo "FAIL: $desc"
    echo "  expected substring: $expect_substr"
    echo "  actual: $actual"
    FAIL=1
  fi
}

# 1) 실제 버그 재현: claude 5h는 97%로 멀쩡, 7d는 56%라 level=red.
#    예전 로직이면 level==red 때문에 SKIP + "5h창 잔여 97% (level=red)"라는
#    앞뒤 안 맞는 문구가 나갔다. 지금은 PROCEED여야 하고, 두 창 다 보이고,
#    level은 "전체상태"로 분리되어 있어야 한다.
OUT=$(run_case '{"providers":{"claude":{"ok":true,"level":"red","reason":"7일 한도가 빠듯함","windows":{"5h":{"left_pct":97},"7d":{"left_pct":56}}}}}' claude)
check "5h 멀쩡+7d red 조합 -> PROCEED(차단 안 됨)" "$OUT" "PROCEED"
check "5h 멀쩡+7d red 조합 -> 5h 수치 보임" "$OUT" "5h창 잔여 97%"
check "5h 멀쩡+7d red 조합 -> 7d 수치도 항상 같이 보임" "$OUT" "7d창 잔여 56%"
check "5h 멀쩡+7d red 조합 -> level은 전체상태로 분리 표기" "$OUT" "전체상태=red"

# 2) 경계값: floor=10 미만이면 차단, 정확히 10이면 차단 안 됨(blocked = pct < floor)
OUT=$(run_case '{"providers":{"claude":{"ok":true,"level":"yellow","reason":"","windows":{"5h":{"left_pct":9},"7d":{"left_pct":80}}}}}' claude)
check "5h=9(<10) -> SKIP" "$OUT" "SKIP:"

OUT=$(run_case '{"providers":{"claude":{"ok":true,"level":"yellow","reason":"","windows":{"5h":{"left_pct":10},"7d":{"left_pct":80}}}}}' claude)
check "5h=10(==floor) -> PROCEED(경계는 차단 아님)" "$OUT" "PROCEED"

# 3) codex는 구조적으로 5h창이 없음 -> N/A로 항상 표시(생략 금지)
OUT=$(run_case '{"providers":{"codex":{"ok":true,"level":"green","reason":"","windows":{"7d":{"left_pct":95}}}}}' codex)
check "codex 5h 없음 -> N/A로 표시" "$OUT" "5h창 잔여 N/A"
check "codex 7d=95 -> PROCEED" "$OUT" "PROCEED"

OUT=$(run_case '{"providers":{"codex":{"ok":true,"level":"red","reason":"","windows":{"7d":{"left_pct":5}}}}}' codex)
check "codex 7d=5(<10) -> SKIP" "$OUT" "SKIP:"

# 4) dual: 둘 다 조회되고, 한쪽만 낮아도 그쪽만 blocker로 잡히는지
OUT=$(run_case '{"providers":{"claude":{"ok":true,"level":"red","reason":"","windows":{"5h":{"left_pct":97},"7d":{"left_pct":56}}},"codex":{"ok":true,"level":"green","reason":"","windows":{"7d":{"left_pct":95}}}}}' dual)
check "dual: claude 5h 멀쩡+codex 7d 멀쩡 -> PROCEED" "$OUT" "PROCEED"
check "dual: claude 정보도 같이 표시" "$OUT" "claude 5h창"
check "dual: codex 정보도 같이 표시" "$OUT" "codex 5h창 잔여 N/A"

OUT=$(run_case '{"providers":{"claude":{"ok":true,"level":"green","reason":"","windows":{"5h":{"left_pct":80},"7d":{"left_pct":90}}},"codex":{"ok":true,"level":"red","reason":"","windows":{"7d":{"left_pct":3}}}}}' dual)
check "dual: claude 멀쩡+codex만 낮음 -> SKIP" "$OUT" "SKIP:"
check "dual: SKIP 사유에 codex 언급" "$OUT" "codex"
# 2026-07-30 fix(Codex 코드리뷰로 발견): 한쪽만 막혀도 안 막힌 provider
# 정보가 SKIP 메시지에서 통째로 사라지던 버그 - claude는 멀쩡한데도 SKIP
# 사유엔 codex만 나오고 claude 정보가 빠지면 안 됨.
check "dual: codex만 막혀도 claude(안 막힌 쪽) 정보는 SKIP에도 그대로 보임" "$OUT" "claude 5h창 잔여 80%"

# 5) provider 자체가 unreadable(ok:false)이면 fail-open, 표시도 아예 없음(진짜 모르는 채로 처리)
OUT=$(run_case '{"providers":{"claude":{"ok":false}}}' claude)
[[ "$OUT" == "PROCEED" ]] && echo "PASS: provider ok:false -> info 없이 순수 PROCEED(fail-open)" || { echo "FAIL: provider ok:false -> info 없이 순수 PROCEED(fail-open)"; echo "  actual: $OUT"; FAIL=1; }

# 5b) 2026-07-30 fix: provider는 ok:true인데 판정 대상 창(window_key) 자체가
#     빠진 경우(coach 일시 결측 등) — 차단 판정만 fail-open이어야 하고,
#     "두 창 항상 표시" 요구사항은 이 경우에도 지켜져야 한다(예전엔 이
#     경우도 정보 자체가 통째로 사라졌다).
OUT=$(run_case '{"providers":{"claude":{"ok":true,"level":"red","reason":"7일 한도가 빠듯함","windows":{"7d":{"left_pct":56}}}}}' claude)
check "claude ok:true인데 5h창만 결측 -> PROCEED(판단 불가는 차단 아님)" "$OUT" "PROCEED"
check "claude ok:true인데 5h창만 결측 -> 그래도 5h는 N/A로 표시" "$OUT" "5h창 잔여 N/A"
check "claude ok:true인데 5h창만 결측 -> 7d는 정상 표시" "$OUT" "7d창 잔여 56%"

# 6) fail-open: coach 출력이 깨졌을 때 SKIP이 아니라 PROCEED여야 함
OUT=$(run_case 'not json at all' claude)
check "coach 출력이 JSON이 아니면 -> PROCEED(fail-open)" "$OUT" "PROCEED"

echo
if [ "$FAIL" -eq 0 ]; then
  echo "판정 로직(fixture) 테스트 전부 통과"
else
  echo "판정 로직(fixture) 테스트 중 실패 있음"
fi

echo
echo "--- 참고: 실제 coach로 셸 래퍼(usage-preflight-gate.sh)가 살아있는지만 확인 (값은 assert 안 함, 매일 바뀜) ---"
bash -n "$GATE_SH" && echo "bash -n 통과"
if command -v coach >/dev/null 2>&1; then
  echo "claude: $(bash "$GATE_SH" claude)"
  echo "codex:  $(bash "$GATE_SH" codex)"
else
  echo "(coach 명령이 이 환경에 없어 생략)"
fi

exit "$FAIL"
