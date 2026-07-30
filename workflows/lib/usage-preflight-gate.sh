#!/usr/bin/env bash
# usage-preflight-gate.sh <claude|codex|dual>
# stdout: "PROCEED" or "SKIP: <reason>" (always exit 0 — decision is in
# stdout, same convention as route-dispatch.sh's "ROUTED-TO: ..." line).
#
# Why this exists (2026-07-28): weekly-report.sh, kakao-morning-briefing.sh,
# and discord-bot.py's claude -p/codex dispatches all fire blind to current
# account usage. On 2026-07-28 alone this caused >=8 independent
# session-limit failures across otherwise-unrelated automations, several
# after 10-30 minutes of a hung/failing run nobody was watching (a scheduled
# launchd job has no human present to notice a hang the way an interactive
# session does). This gate is a cheap pre-check reusing the SAME `coach`
# data usage-advisor.sh/route-dispatch.sh already read — refuse to start if
# the specific window that failure mode depends on is already critically
# low, instead of finding out 26 minutes into a doomed run.
#
# Deliberately NOT built on coach-headroom.sh: that helper collapses
# everything to two bare integers (claude/codex), which is exactly right for
# usage-advisor.sh's "which is bigger" comparison but throws away `level`
# and `reason`, which this gate wants for a human-readable skip message.
# Re-parses `coach --json` directly instead, with the same
# never-string-interpolate-JSON-into-python-source discipline
# coach-headroom.sh documents (piped via stdin here too).
#
# Threshold: SKIP only if the checked window's own left_pct < FLOOR_PCT.
# Claude is checked on its 5h window specifically (not 7d) — that's the
# window whose exhaustion is what actually causes a mid-run failure; the 7d
# window can be comfortably high while 5h is at 0%, exactly as observed live
# on 2026-07-28 (claude 5h=0%, 7d=83%, level=yellow). Codex only has a 7d
# window (coach has no visibility into antigravity at all, same reason
# coach-headroom.sh never reports it — "dual" here means claude+codex only).
#
# 2026-07-30 fix (실측으로 발견): 예전엔 여기서 coach의 provider-level
# `level`(red/yellow/green)도 OR 조건으로 같이 봤다. 근데 `level`은 provider
# 전체를 대표하는 단일 값이라 windows 중 가장 빠듯한 쪽이 좌우한다 — claude
# 5h=97%(멀쩡)인데도 7d=56%라 level=red가 되면, 검사 대상은 5h인데 엉뚱한
# 창이 만든 level 때문에 SKIP되고 메시지엔 "5h창 잔여 98% (level=red)"라는
# 앞뒤 안 맞는 조합이 나갔다. 지금은 SKIP 여부를 window_key 자신의
# left_pct만으로 결정한다.
#
# 표시 형식(1차 수정 후 리뷰 피드백 반영, 2026-07-30 2차): 두 창(5h/7d)을
# "항상" 함께 보여준다 — 예전엔 다른 창 데이터가 있을 때만 덧붙여서, codex처럼
# 구조적으로 5h창 자체가 없는 provider에서는 여전히 한쪽만 보였다. 지금은
# 없는 창도 "N/A"로 명시한다. 그리고 `level`/`reason`은 특정 창이 아니라
# provider 전체를 요약한 값이라는 걸 "전체상태="라는 라벨로 분리해서, 두 창
# 잔여율 뒤에만 붙인다 — "5h창 98%인데 왜 전체상태=red냐"는 오해가 다시
# 나오지 않도록, 어느 창 숫자에도 level을 직접 매달지 않는다(사용자 요청:
# 둘 다 알려주는 방향으로, 차단 여부와는 분리해서 정보로).
#
# Fail-open, not fail-closed: if coach is missing, errors, or returns
# unparseable/incomplete data, this prints PROCEED — an unreachable checker
# must not become a silent kill-switch for every scheduled automation on
# this machine. Same "0/unknown is not the same as confirmed depleted"
# stance coach-headroom.sh already documents.
set -uo pipefail

export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

ACTOR="${1:?usage: usage-preflight-gate.sh <claude|codex|dual>}"
FLOOR_PCT=10

if ! command -v coach >/dev/null 2>&1; then
  echo "PROCEED (coach unavailable — gate skipped, not enforced)"
  exit 0
fi

# --providers로 실제 필요한 provider만 조회(2026-07-30, 사용자 요청으로 콕스
# 응답 지연을 파다가 발견): coach --json은 기본적으로 claude/codex/antigravity
# 세 provider를 codexbar로 "순차적으로"(gather()의 plain for 루프, 병렬 아님)
# 조회한다 — codexbar 단발 호출이 ~1.5~2초라 3개면 ~6초가 그대로 이 게이트의
# 지연으로 누적된다. 실측: coach --json(3개 전부)=6.3~6.4초,
# --providers codex 하나만=1.9초, --providers claude 하나만=1.8초 — 약 4.5초
# 절감. 이 게이트는 ACTOR 하나(또는 dual일 때 claude+codex 둘)만 실제로
# 쓰므로, 처음부터 그것만 조회하도록 범위를 좁힌다. antigravity는 원래도 이
# 게이트가 안 쓰므로(위 주석 "coach has no visibility into antigravity at
# all" 참고) 어느 ACTOR 값에서도 요청하지 않는다.
case "$ACTOR" in
  claude) COACH_PROVIDERS="claude" ;;
  codex) COACH_PROVIDERS="codex" ;;
  dual) COACH_PROVIDERS="claude,codex" ;;
  *) COACH_PROVIDERS="claude,codex" ;;  # 알 수 없는 ACTOR — fail-open으로 필요한 둘 다 조회
esac

COACH_OUTPUT="$(coach --json --providers "$COACH_PROVIDERS" 2>/dev/null)"
if [ -z "$COACH_OUTPUT" ]; then
  echo "PROCEED (coach returned no data — gate skipped, not enforced)"
  exit 0
fi

# 판정 로직은 usage-preflight-gate.py로 분리(2026-07-30) — bash heredoc
# 안에 python 문자열을 직접 박아두면 따옴표 이스케이프가 계속 꼬이고, 무엇보다
# fixture JSON으로 직접 단위 테스트하기 어려웠다(usage-preflight-gate.test.sh
# 참고).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
printf '%s' "$COACH_OUTPUT" | python3 "$SCRIPT_DIR/usage-preflight-gate.py" "$ACTOR" "$FLOOR_PCT"
