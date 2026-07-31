#!/usr/bin/env bash
# PostToolUse(Workflow) observer.  Only a successful verify-task-v2 result
# opens the main-session edit gate; invocation alone is not sufficient.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hooks/lib/verify-task-state.sh
source "$SCRIPT_DIR/lib/verify-task-state.sh"

INPUT="$(cat)"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)"
CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)"
TOOL_NAME="$(printf '%s' "$INPUT" | jq -r '.tool_name // .name // empty' 2>/dev/null)"
SCRIPT_PATH="$(printf '%s' "$INPUT" | jq -r '.tool_input.scriptPath // .input.scriptPath // empty' 2>/dev/null)"
WORKFLOW_NAME="$(printf '%s' "$INPUT" | jq -r '.tool_input.name // .input.name // empty' 2>/dev/null)"
WORKFLOW_CWD="$(printf '%s' "$INPUT" | jq -r '.tool_input.args.cwd // .input.args.cwd // empty' 2>/dev/null)"
WORKFLOW_TASK="$(printf '%s' "$INPUT" | jq -r '.tool_input.args.task // .input.args.task // empty' 2>/dev/null)"

[ -n "$SESSION_ID" ] && [ -n "$CWD" ] || exit 0
if [ -n "$TOOL_NAME" ] && [ "$TOOL_NAME" != "Workflow" ]; then
  exit 0
fi
if ! printf '%s\n%s' "$SCRIPT_PATH" "$WORKFLOW_NAME" | grep -Eq '(^|/)verify-task(-v2)?(-wf_[A-Za-z0-9_-]+)?\.js$|^verify-task(-v2)?$'; then
  exit 0
fi
# The workflow must have been given the same checkout and a concrete task.
# Otherwise a successful run in another repository could unlock this session.
[ "$WORKFLOW_CWD" = "$CWD" ] && [ -n "$WORKFLOW_TASK" ] || exit 0

STATE_JSON="$(verify_task_state_read "$SESSION_ID" 2>/dev/null || true)"
PROMPT_HASH="$(printf '%s' "$STATE_JSON" | jq -r '.prompt_hash // empty' 2>/dev/null)"
CURRENT_STATE_CWD="$(printf '%s' "$STATE_JSON" | jq -r '.cwd // empty' 2>/dev/null)"
[ "$CURRENT_STATE_CWD" = "$CWD" ] && [ -n "$PROMPT_HASH" ] || exit 0

# Recursively inspect structuredContent/content/text/tool_response without
# trusting a provider-specific wrapper.  A JSON string is decoded once so
# textual Workflow responses work too.  Unknown or malformed responses fail
# closed and leave the gate locked.
PASSED="$(printf '%s' "$INPUT" | jq -r '
  def contains_passed:
    if type == "object" then
      ((.finalVerdict?.passed? == true) or ([.[]? | contains_passed] | any))
    elif type == "array" then
      any(.[]; contains_passed)
    elif type == "string" then
      (try (fromjson | contains_passed) catch false)
    else false end;
  if (.tool_response? // null) | contains_passed then "true" else "false" end
' 2>/dev/null)"

if [ "$PASSED" = "true" ]; then
  STATUS="workflow_completed"
  WORKFLOW_PASSED=true
else
  STATUS="workflow_failed"
  WORKFLOW_PASSED=false
fi

NEW_STATE="$(printf '%s' "$STATE_JSON" | jq \
  --arg status "$STATUS" \
  --arg path "$SCRIPT_PATH" \
  --arg name "$WORKFLOW_NAME" \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson passed "$WORKFLOW_PASSED" \
  '. + {status:$status, workflow_path:$path, workflow_name:$name, workflow_passed:$passed, completed_at:$completed_at}')"
verify_task_state_write "$SESSION_ID" "$NEW_STATE" || true
exit 0
