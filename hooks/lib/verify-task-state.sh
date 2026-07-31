#!/usr/bin/env bash
# Shared state helpers for the verify-task-v2 automatic entry gate.
#
# The state is deliberately small and contains no raw prompt text.  A session
# id, cwd, and prompt hash prevent a successful run from being reused by a
# later prompt or a different checkout.

set -uo pipefail

VERIFY_TASK_STATE_ROOT="${VERIFY_TASK_STATE_ROOT:-${HOME}/.claude/hooks-state/verify-task-v2}"

verify_task_safe_session_id() {
  local session_id="${1:-}"
  session_id="$(printf '%s' "$session_id" | tr -c 'A-Za-z0-9_.-' '_')"
  printf '%s' "${session_id:-unknown-session}"
}

verify_task_state_file() {
  local session_id
  session_id="$(verify_task_safe_session_id "${1:-}")"
  printf '%s/%s.json' "$VERIFY_TASK_STATE_ROOT" "$session_id"
}

verify_task_hash() {
  local value="${1:-}"
  if command -v shasum >/dev/null 2>&1; then
    printf '%s' "$value" | shasum -a 256 | awk '{print $1}'
  else
    printf '%s' "$value" | cksum | awk '{print $1}'
  fi
}

verify_task_state_read() {
  local session_id="${1:-}"
  local file
  file="$(verify_task_state_file "$session_id")"
  [ -f "$file" ] || return 1
  jq -e . "$file" 2>/dev/null
}

verify_task_state_write() {
  local session_id="${1:-}"
  local json="${2:-}"
  local file lock_dir tmp acquired=0 attempt

  [ -n "$session_id" ] || return 1
  printf '%s' "$json" | jq -e . >/dev/null 2>&1 || return 1

  mkdir -p "$VERIFY_TASK_STATE_ROOT/locks" || return 1
  file="$(verify_task_state_file "$session_id")"
  lock_dir="${file}.lock"

  for attempt in $(seq 1 40); do
    if mkdir "$lock_dir" 2>/dev/null; then
      acquired=1
      break
    fi
    # A killed hook can leave a mkdir-based lock behind.  The critical
    # section is sub-second in normal operation, so an older lock is safe to
    # recover while a live writer remains protected.
    if [ -d "$lock_dir" ] && [ -n "$(find "$lock_dir" -maxdepth 0 -type d -mmin +2 -print -quit 2>/dev/null)" ]; then
      rmdir "$lock_dir" 2>/dev/null || true
    fi
    sleep 0.025
  done
  [ "$acquired" -eq 1 ] || return 1

  tmp="${file}.tmp.$$.$RANDOM"
  trap 'rm -f "$tmp" 2>/dev/null; rmdir "$lock_dir" 2>/dev/null || true' RETURN
  if printf '%s' "$json" > "$tmp" && mv -f "$tmp" "$file"; then
    trap - RETURN
    rmdir "$lock_dir" 2>/dev/null || true
    return 0
  fi

  trap - RETURN
  rm -f "$tmp" 2>/dev/null || true
  rmdir "$lock_dir" 2>/dev/null || true
  return 1
}

verify_task_state_is_current() {
  local json="${1:-}"
  local cwd="${2:-}"
  local prompt_hash="${3:-}"
  [ -n "$json" ] || return 1
  jq -e --arg cwd "$cwd" --arg prompt_hash "$prompt_hash" '
    (.cwd == $cwd) and
    (.prompt_hash == $prompt_hash) and
    (.status == "gate_required" or .status == "workflow_started" or .status == "workflow_failed")
  ' <<<"$json" >/dev/null 2>&1
}
