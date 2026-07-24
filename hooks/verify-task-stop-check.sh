#!/usr/bin/env bash
# Stop hook: nags (once per session) if this session did substantive work
# (several Edit/Write calls, or a risky Bash command) without any
# verify-task invocation appearing in the transcript.
#
# This is a MECHANICAL PROXY, not real judgment. A shell script can't tell a
# trivial edit from an important one — it only counts Edit/Write calls and
# greps Bash commands against a keyword list. It exists to stop "I forgot to
# verify" from happening silently, not to replace judgment about what counts
# as substantive. It nags at most once per session_id (a marker file), so it
# can't loop forever if the answer is genuinely "this didn't need it."
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
HAS_VERIFY_TASK="$(grep -c 'verify-task' "$TRANSCRIPT_PATH" 2>/dev/null | tr -d ' ')"

SUBSTANTIVE=0
[ "${EDIT_WRITE_COUNT:-0}" -ge 3 ] && SUBSTANTIVE=1
[ "${RISKY_BASH_COUNT:-0}" -ge 1 ] && SUBSTANTIVE=1

if [ "$SUBSTANTIVE" -eq 1 ] && [ "${HAS_VERIFY_TASK:-0}" -eq 0 ]; then
  touch "$NAG_MARKER"
  jq -n '{
    decision: "block",
    reason: "이 세션에서 파일 수정/설치 등 실질적인 작업 신호가 감지됐는데, verify-task로 독립 검증한 흔적이 안 보입니다. 이번 작업이 검증 대상(중요/복잡한 작업)이라면 Workflow({scriptPath:\"~/.claude/workflows/verify-task.js\", args:{task, result, persona, cwd}})를 돌리세요. 검증이 필요 없는 가벼운 작업이었다면, 그 이유를 사용자에게 한 줄로 알려주고 종료하세요.",
    systemMessage: "verify-task 미실행 감지 — 검증하거나, 왜 생략하는지 사용자에게 알려주세요."
  }'
fi

exit 0
