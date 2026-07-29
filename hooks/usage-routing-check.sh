#!/usr/bin/env bash
# Stop hook: nags (once per session) if Claude did substantial direct work
# (>=3 Edit/Write calls) while Claude's OWN usage was constrained (coach
# level yellow/red) with no evidence of consulting the routing policy in
# docs/usage-routing.md — route-dispatch.sh (Rule B / policy points 2-3),
# usage-advisor.sh (Rule 1's deterministic comparison), codex-execute-dispatch.sh,
# or score-dispatch.sh.
#
# Rule A (docs/usage-routing.md, objectified 2026-07-27) now only exempts two
# mechanically-checkable cases, both grepped for below before nagging:
#   1. an independent-verification skill/workflow ran this session
#      (verify-task / verify-task-v2 / independent-critique-loop)
#   2. a mcp__claude-in-chrome__* browser-automation tool was actually used
# Still a proxy, same limitation as verify-task-stop-check.sh — string
# matching can false-positive (skill loaded but not actually load-bearing
# for the direct edits) or miss paraphrased invocations. But the old fully
# subjective "orchestrator judged this needed me" excuse no longer passes.
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

# Parsed with jq (already a hard dependency here), same reasoning as
# HAS_VERIFY_SKILL below: raw-JSON key grepping breaks silently if the
# transcript serializer's whitespace/key-ordering ever shifts.
EDIT_WRITE_COUNT="$(jq -r '
  select(.type=="assistant") | .message.content[]? | select(.type=="tool_use") |
  select(.name=="Edit" or .name=="Write") | .name
' "$TRANSCRIPT_PATH" 2>/dev/null | wc -l | tr -d ' ')"
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

# Rule A objective exceptions — if either is present, this session is
# exempt from routing by design; skip the nag entirely.
#
# HAS_VERIFY_SKILL fixed 2026-07-29: the old pattern grepped for the literal
# key `"skill":"verify-task"`, which is not how a real invocation is ever
# recorded — a real-world audit of every session log for this project found
# that key appearing zero times, while the bare word "verify-task" appears
# constantly as boilerplate (skill_listing descriptions, docs paths) that
# has nothing to do with the skill actually running. Per docs/verify-task.md
# ("Usage: `Workflow({scriptPath: "workflows/verify-task.js", ...})`"),
# verify-task/verify-task-v2 run as a `Workflow` tool_use with a `scriptPath`
# input; independent-critique-loop is a real Skill and would show as a
# `Skill` tool_use with that name in its input. Parsed with jq (already a
# hard dependency here) instead of raw grep, since jq walks the actual
# message.content[].{type,name,input} structure rather than guessing at key
# ordering/whitespace in the serialized JSON.
HAS_VERIFY_SKILL="$(jq -r '
  select(.type=="assistant") | .message.content[]? | select(.type=="tool_use") |
  if .name == "Workflow" then (.input.scriptPath // "")
  elif .name == "Skill" then (.input.skill // .input.name // "")
  else empty end
' "$TRANSCRIPT_PATH" 2>/dev/null | grep -c 'verify-task\|independent-critique-loop')"
HAS_BROWSER_TOOL="$(jq -r '
  select(.type=="assistant") | .message.content[]? | select(.type=="tool_use") |
  select(.name | startswith("mcp__claude-in-chrome__")) | .name
' "$TRANSCRIPT_PATH" 2>/dev/null | wc -l | tr -d ' ')"
if [ "${HAS_VERIFY_SKILL:-0}" -gt 0 ] || [ "${HAS_BROWSER_TOOL:-0}" -gt 0 ]; then
  touch "$NAG_MARKER"
  exit 0
fi

HAS_ROUTING="$(grep -c 'route-dispatch\.sh\|usage-advisor\.sh\|codex-execute-dispatch\.sh\|score-dispatch\.sh' "$TRANSCRIPT_PATH" 2>/dev/null | tr -d ' ')"
if [ "${HAS_ROUTING:-0}" -eq 0 ]; then
  touch "$NAG_MARKER"
  jq -n --arg level "$CLAUDE_LEVEL" '{
    decision: "block",
    reason: ("클로드 자체 사용량이 낮은 상태(coach level=" + $level + ")에서 Edit/Write 3회 이상의 직접 작업이 있었는데, docs/usage-routing.md 정책을 따른 흔적이 안 보입니다. 코덱스 가능 작업은 workflows/lib/usage-advisor.sh로 비교 후 우세한 쪽에, 단순 작업은 workflows/lib/route-dispatch.sh로. Rule A 예외(독립검사 스킬 실행, 또는 mcp__claude-in-chrome 브라우저 자동화)에 해당하지 않는다면 지금이라도 라우팅을 타세요."),
    systemMessage: "사용량 라우팅 정책 미준수 감지 — usage-advisor.sh/route-dispatch.sh 사용을 고려하세요."
  }'
fi

exit 0
