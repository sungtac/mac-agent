#!/usr/bin/env bash
# Stop hook: nags (once per session) if this session did substantive work
# without any verify-task invocation appearing in the transcript.
#
# This is a MECHANICAL PROXY, not real judgment. A shell script can't tell a
# trivial edit from an important one — it only counts tool calls and greps
# Bash commands against a keyword list.
#
# Two tiers:
#   - MANDATORY category registry (2026-07-27, user-confirmed): if a session
#     matches one of these, verify-task MUST have run — no "explain why you
#     skipped it" escape. Grown one item at a time as the user identifies
#     more (see docs/verify-task-stop-check.md):
#       1. 코딩 (coding)     — 3+ Edit/Write tool calls this session.
#       2. 아이디어 회의 (idea/design meeting) — ExitPlanMode invoked this
#          session (best mechanical proxy available right now — a design
#          discussion that never enters Plan Mode isn't caught by this yet;
#          known gap, not a solved detector).
#   - SOFT category (pre-existing, escape valve intact): a risky Bash
#     command (git commit/push, brew/npm/pip install, plugin install,
#     marketplace add, rm -rf, chmod +x) with no mandatory-category hit —
#     agent may explain to the user why verification was skipped instead.
#
# It nags at most once per session_id (a marker file), so it can't loop
# forever if the answer is genuinely "this didn't need it" (soft tier only).
set -uo pipefail

INPUT="$(cat)"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)"
TRANSCRIPT_PATH="$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)"

[ -z "$SESSION_ID" ] && exit 0

STATE_DIR="$HOME/.claude/hooks-state/verify-task-nag"
mkdir -p "$STATE_DIR"
NAG_MARKER="$STATE_DIR/${SESSION_ID}.nagged"

# housekeeping: prune markers older than a week
find "$STATE_DIR" -type f -mtime +7 -delete 2>/dev/null || true

# already nagged this session once -> let it stop
[ -f "$NAG_MARKER" ] && exit 0

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  TRANSCRIPT_PATH="$(find "$HOME/.claude/projects" -name "${SESSION_ID}.jsonl" 2>/dev/null | head -1)"
fi
if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  exit 0
fi

EDIT_WRITE_COUNT="$(grep -o '"name":"\(Edit\|Write\)"' "$TRANSCRIPT_PATH" 2>/dev/null | wc -l | tr -d ' ')"
RISKY_BASH_COUNT="$(grep -oE '"command":"[^"]*(git commit|git push|brew install|npm install|pip install|pip3 install|plugin install|marketplace add|rm -rf|chmod \+x)[^"]*"' "$TRANSCRIPT_PATH" 2>/dev/null | wc -l | tr -d ' ')"
HAS_PLAN_MODE="$(grep -c '"name":"ExitPlanMode"' "$TRANSCRIPT_PATH" 2>/dev/null | tr -d ' ')"
HAS_VERIFY_TASK="$(grep -c 'verify-task' "$TRANSCRIPT_PATH" 2>/dev/null | tr -d ' ')"

[ "${HAS_VERIFY_TASK:-0}" -gt 0 ] && exit 0

MANDATORY_HITS=()
[ "${EDIT_WRITE_COUNT:-0}" -ge 3 ] && MANDATORY_HITS+=("코딩(Edit/Write ${EDIT_WRITE_COUNT}회)")
[ "${HAS_PLAN_MODE:-0}" -gt 0 ] && MANDATORY_HITS+=("아이디어 회의(ExitPlanMode 사용)")

if [ "${#MANDATORY_HITS[@]:-0}" -gt 0 ]; then
  touch "$NAG_MARKER"
  HITS_STR="$(IFS=', '; echo "${MANDATORY_HITS[*]}")"
  jq -n --arg hits "$HITS_STR" '{
    decision: "block",
    reason: ("이 세션은 무조건 독립검증 대상 카테고리(" + $hits + ")에 해당하는데 verify-task 실행 흔적이 없습니다. 이 카테고리는 \"검증 필요 없었다\"는 설명으로 넘어갈 수 없습니다 — Workflow({scriptPath:\"~/.claude/workflows/verify-task.js\", args:{task, result, persona, cwd}})를 실제로 돌리세요."),
    systemMessage: "무조건 독립검증 카테고리 감지 — verify-task 실행 필수, 생략 불가."
  }'
  exit 0
fi

if [ "${RISKY_BASH_COUNT:-0}" -ge 1 ]; then
  touch "$NAG_MARKER"
  jq -n '{
    decision: "block",
    reason: "이 세션에서 설치/커밋 등 위험 신호가 있는 Bash 명령이 감지됐는데, verify-task로 독립 검증한 흔적이 안 보입니다. 이번 작업이 검증 대상(중요/복잡한 작업)이라면 Workflow({scriptPath:\"~/.claude/workflows/verify-task.js\", args:{task, result, persona, cwd}})를 돌리세요. 검증이 필요 없는 가벼운 작업이었다면, 그 이유를 사용자에게 한 줄로 알려주고 종료하세요.",
    systemMessage: "verify-task 미실행 감지 — 검증하거나, 왜 생략하는지 사용자에게 알려주세요."
  }'
fi

exit 0
