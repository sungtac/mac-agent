#!/usr/bin/env bash
# PreToolUse(Edit|Write) gate for the main Claude session.
# Internal subagents are allowed to write their isolated temporary artifacts;
# the outer session must have a successful verify-task-v2 result first.
#
# Fast-path (2026-08-08): a short allowlist of personal shell dotfiles
# directly under $HOME, when $HOME is not itself a git work tree, skips the
# heavy multi-model verify-task-orchestrator and instead takes a timestamped
# backup + lets the edit through immediately. hooks/verify-task-dotfile-fastpath-check.sh
# (PostToolUse) then syntax-checks the result and auto-restores the backup on
# failure. Anything inside a git repo (including this repo) still requires
# the full gate below.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hooks/lib/verify-task-state.sh
source "$SCRIPT_DIR/lib/verify-task-state.sh"

INPUT="$(cat)"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)"
CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)"
AGENT_ID="$(printf '%s' "$INPUT" | jq -r '.agent_id // empty' 2>/dev/null)"
FILE_PATH="$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"

# Claude may report a temporary-directory path through a symlink (for example
# /tmp vs /private/tmp on macOS), while the host orchestrator records the
# canonical path from Path.resolve(). Compare the same representation on both
# sides so a valid run is not rejected for an equivalent path spelling.
if [ -n "$CWD" ]; then
  CWD_CANONICAL="$(cd -- "$CWD" 2>/dev/null && pwd -P || true)"
  [ -n "$CWD_CANONICAL" ] && CWD="$CWD_CANONICAL"
fi

# Claude Code runs session hooks inside subagents as well.  Without this
# bypass, verify-task-v2's own temporary Write operations deadlock the gate.
[ -n "$AGENT_ID" ] && exit 0

# --- Fast-path: personal shell dotfiles, outside any git repo -------------
DOTFILE_FASTPATH_RE='^'"${HOME}"'/\.(zshenv|zshrc|zprofile|bashrc|bash_profile)$'
if [ -n "$FILE_PATH" ] && [[ "$FILE_PATH" =~ $DOTFILE_FASTPATH_RE ]]; then
  if ! git -C "$HOME" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    BACKUP_DIR="$HOME/.claude/hooks-state/dotfile-fastpath-backups"
    mkdir -p "$BACKUP_DIR" 2>/dev/null || true
    if [ -f "$FILE_PATH" ]; then
      cp -p "$FILE_PATH" "$BACKUP_DIR/$(basename "$FILE_PATH").$(date +%Y%m%d%H%M%S).bak" 2>/dev/null || true
    fi
    exit 0
  fi
fi
# ---------------------------------------------------------------------------

if [ -z "$SESSION_ID" ] || [ -z "$CWD" ]; then
  jq -n ' {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: "세션 식별 정보(session_id/cwd)를 확인할 수 없어 메인 Edit/Write를 차단했습니다. 세션을 다시 시작한 뒤 verify-task-v2를 실행하세요."
    }
  }'
  exit 0
fi

STATE_JSON="$(verify_task_state_read "$SESSION_ID" 2>/dev/null || true)"
if printf '%s' "$STATE_JSON" | jq -e --arg cwd "$CWD" '
  (.cwd == $cwd) and (.status == "workflow_completed") and (.workflow_passed == true)
' >/dev/null 2>&1; then
  exit 0
fi

jq -n ' {
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: "메인 세션의 Edit/Write를 차단했습니다. 먼저 Bash로 /Users/edge_ai/mac-agent/bin/verify-task-orchestrator.py --task-file <작업파일> --cwd <cwd> --session-id <session_id> 를 실행하고, 호스트 하네스와 독립 리뷰가 성공한 뒤 다시 시도하세요."
  }
}'