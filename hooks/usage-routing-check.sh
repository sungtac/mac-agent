#!/usr/bin/env bash
# Stop hook: nags (once per session) if Claude did substantial direct work
# (>=3 Edit/Write calls) while Claude's OWN usage was constrained (coach
# level yellow/red) with no evidence of consulting the routing policy in
# docs/usage-routing.md — route-dispatch.sh (Rule B / policy points 2-3),
# usage-advisor.sh (Rule 1's deterministic comparison), codex-execute-dispatch.sh,
# or score-dispatch.sh.
#
# Mechanical proxy only, same limitation as verify-task-stop-check.sh — a
# shell script can't judge whether any SPECIFIC action was actually
# delegable (e.g. live browser automation via mcp__claude-in-chrome can't be
# routed to codex at all), and can't verify policy Rule A (Orchestrator's own
# unique-judgment work is exempt) was correctly invoked rather than used as
# an excuse. It nags; it doesn't and can't block correctly.
set -uo pipefail

export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

INPUT="$(cat)"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)"
TRANSCRIPT_PATH="$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)"

[ -z "$SESSION_ID" ] && exit 0

STATE_DIR="$HOME/.claude/hooks-state/usage-routing-nag"
mkdir -p "$STATE_DIR"
NAG_MARKER="$STATE_DIR/${SESSION_ID}.nagged"
find "$STATE_DIR" -type f -mtime +7 -delete 2>/dev/null || true
[ -f "$NAG_MARKER" ] && exit 0

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  TRANSCRIPT_PATH="$(find "$HOME/.claude/projects" -name "${SESSION_ID}.jsonl" 2>/dev/null | head -1)"
fi
if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  exit 0
fi

EDIT_WRITE_COUNT="$(grep -o '"name":"\(Edit\|Write\)"' "$TRANSCRIPT_PATH" 2>/dev/null | wc -l | tr -d ' ')"
[ "${EDIT_WRITE_COUNT:-0}" -lt 3 ] && exit 0

# Only fires if Claude's own usage was actually constrained this session —
# no point nagging when there was plenty of headroom to spend directly.
CLAUDE_LEVEL="unknown"
if command -v coach >/dev/null 2>&1; then
  CLAUDE_LEVEL="$(coach --json 2>/dev/null | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin)['providers']['claude']['level'])
except Exception:
    print('unknown')
" 2>/dev/null)"
fi
case "$CLAUDE_LEVEL" in
  yellow|red) : ;;
  *) exit 0 ;;
esac

HAS_ROUTING="$(grep -c 'route-dispatch\.sh\|usage-advisor\.sh\|codex-execute-dispatch\.sh\|score-dispatch\.sh' "$TRANSCRIPT_PATH" 2>/dev/null | tr -d ' ')"
if [ "${HAS_ROUTING:-0}" -eq 0 ]; then
  touch "$NAG_MARKER"
  jq -n --arg level "$CLAUDE_LEVEL" '{
    decision: "block",
    reason: ("클로드 자체 사용량이 낮은 상태(coach level=" + $level + ")에서 Edit/Write 3회 이상의 직접 작업이 있었는데, docs/usage-routing.md 정책(2026-07-26)을 따른 흔적이 안 보입니다. 코덱스 가능 작업은 workflows/lib/usage-advisor.sh로 비교 후 우세한 쪽에, 단순 작업은 workflows/lib/route-dispatch.sh로. 이번 세션이 정책 예외(Rule A: 오케스트레이터 고유 판단, 또는 브라우저 조작 등 라우팅 불가능한 도구)였다면 사용자에게 한 줄로 알려주고 종료하세요."),
    systemMessage: "사용량 라우팅 정책 미준수 감지 — usage-advisor.sh/route-dispatch.sh 사용을 고려하세요."
  }'
fi

exit 0
