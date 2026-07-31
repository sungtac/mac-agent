#!/usr/bin/env bash
# Bounded runner for the real Claude -> verify-task-v2 nano pilot.
#
# This is an operational harness, not part of the Workflow gate itself. The
# outer `claude -p` process can hang before verify-task-v2 returns, so the
# caller needs a watchdog independent of the Workflow's own agent calls.
# Default behavior is deliberately one attempt: session/quota failures are
# not made worse by blind retries. Set NANO_PILOT_MAX_ATTEMPTS > 1 only when
# retrying transient timeout/network failures is intentional.
set -uo pipefail

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo "usage: run-nano-provider-pilot.sh <git-cwd> <prompt-file> [event-file]" >&2
  exit 64
fi

CWD="$1"
PROMPT_FILE="$2"
EVENT_FILE="${3:-${NANO_EVENT_FILE:-}}"
# A real nano Workflow includes planning, execution, independent review and
# event recording. 300s was empirically too close to the normal path and
# turned a slow-but-live run into a false timeout. Keep the bound finite, but
# leave enough room for one complete provider round.
TIMEOUT_SECONDS="${NANO_PILOT_TIMEOUT_SECONDS:-600}"
MAX_ATTEMPTS="${NANO_PILOT_MAX_ATTEMPTS:-1}"
LOG_DIR="${NANO_PILOT_LOG_DIR:-${TMPDIR:-/tmp}/nano-provider-pilot-logs}"
USAGE_SNAPSHOT_SCRIPT="${NANO_USAGE_SNAPSHOT_SCRIPT:-}"
USAGE_SNAPSHOT_FILE="${NANO_USAGE_SNAPSHOT_FILE:-}"
# Claude Code otherwise gives up waiting for Workflow agent() calls and
# detaches them in the background, which is indistinguishable from a false
# successful `claude -p` pilot. The production verify-task retry path already
# relies on this setting; the standalone pilot must carry the same contract.
CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS="${CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS:-0}"
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS

case "$TIMEOUT_SECONDS" in
  ''|*[!0-9]*) echo "NANO_PILOT_TIMEOUT_SECONDS must be a positive integer" >&2; exit 64 ;;
esac
case "$MAX_ATTEMPTS" in
  ''|*[!0-9]*) echo "NANO_PILOT_MAX_ATTEMPTS must be a positive integer" >&2; exit 64 ;;
esac
if [ "$TIMEOUT_SECONDS" -le 0 ] || [ "$MAX_ATTEMPTS" -le 0 ]; then
  echo "timeout and max attempts must be positive" >&2
  exit 64
fi
if [ ! -d "$CWD" ] || ! /usr/bin/git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "pilot cwd is not a git repository: $CWD" >&2
  exit 66
fi
if [ ! -f "$PROMPT_FILE" ]; then
  echo "pilot prompt file not found: $PROMPT_FILE" >&2
  exit 66
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../workflows/lib/provider-bin.sh
. "$SCRIPT_DIR/../workflows/lib/provider-bin.sh"
PROVIDER_SANDBOX="$SCRIPT_DIR/edge-agent-provider-sandbox.sh"
REQUIRE_USAGE_DATA="${NANO_PILOT_REQUIRE_USAGE_DATA:-1}"
USAGE_GATE="${NANO_PILOT_USAGE_GATE:-$SCRIPT_DIR/../workflows/lib/usage-preflight-gate.sh}"
if [ "$REQUIRE_USAGE_DATA" = "1" ]; then
  if [ ! -x "$USAGE_GATE" ]; then
    echo 'status=usage_gate_unavailable'
    echo "reason=usage gate is missing or not executable: $USAGE_GATE"
    exit 76
  fi
  PREFLIGHT_OUTPUT="$(bash "$USAGE_GATE" dual 2>&1)"
  if printf '%s' "$PREFLIGHT_OUTPUT" | grep -Eiq 'gate skipped|coach unavailable|no data|unparseable|incomplete'; then
    echo 'status=usage_data_unavailable'
    echo 'reason=provider usage data was not confirmed; pilot was not started'
    exit 76
  fi
  if printf '%s' "$PREFLIGHT_OUTPUT" | grep -q '^SKIP:'; then
    echo 'status=provider_unavailable'
    echo 'reason=usage preflight blocked the pilot'
    exit 75
  fi
fi
CLAUDE_BIN="${CLAUDE_BIN:-}"
[ -n "$CLAUDE_BIN" ] || CLAUDE_BIN="$(find_claude_bin || true)"
if [ -z "$CLAUDE_BIN" ] || [ ! -x "$CLAUDE_BIN" ]; then
  echo "pilot Claude executable not found (set CLAUDE_BIN to an absolute path)" >&2
  exit 69
fi

mkdir -p "$LOG_DIR"
PROMPT_TEXT="$(cat "$PROMPT_FILE")"
# The Workflow has its own nanoEventFile argument while this outer runner has
# a positional event file. A mismatch makes the runner report bytes for one
# file while the Workflow records into another. Refuse this configuration
# before spending provider usage.
if [ -n "$EVENT_FILE" ]; then
  CONFIG_CHECK="$(python3 - "$PROMPT_TEXT" "$CWD" "$EVENT_FILE" <<'PY'
import re, sys
prompt, cwd, event = sys.argv[1:]
checks = ((r'nanoEventFile\s*:\s*["\x27]([^"\x27]+)', event, 'nanoEventFile'),
          (r'cwd\s*:\s*["\x27]([^"\x27]+)', cwd, 'cwd'))
for pattern, expected, label in checks:
    match = re.search(pattern, prompt)
    if match and match.group(1) != expected:
        print(f'{label} mismatch: prompt={match.group(1)} runner={expected}')
        raise SystemExit(1)
PY
)"
  if [ "$?" -ne 0 ]; then
    echo 'status=configuration_error'
    echo "reason=$CONFIG_CHECK"
    exit 67
  fi
fi
EVENT_BYTES_BEFORE=0
if [ -n "$EVENT_FILE" ] && [ -f "$EVENT_FILE" ]; then
  EVENT_BYTES_BEFORE="$(wc -c < "$EVENT_FILE" | tr -d ' ')"
fi

classify_output() {
  local output_file="$1"
  if grep -Eiq 'session limit|rate limit|quota exceeded|HTTP 429|status code: 429|\"code\"[[:space:]]*:[[:space:]]*429|too many requests' "$output_file"; then
    printf '%s' 'provider_unavailable'
  else
    printf '%s' 'failed'
  fi
}

classify_workflow_output() {
  local output_file="$1"
  # Claude Code can exit 0 after handing Workflow to its background manager.
  # A standalone pilot has no durable waiter for that manager, so this is not
  # success and must never be reported as one.
  if grep -Eiq 'Workflow .* is running in the background|running in the background against' "$output_file"; then
    printf '%s' 'workflow_detached'
    return
  fi
  if grep -Eiq '"finalVerdict"[[:space:]]*:[[:space:]]*\{[^}]*"passed"[[:space:]]*:[[:space:]]*false|"passed"[[:space:]]*:[[:space:]]*false|"error"[[:space:]]*:[[:space:]]*"nano_' "$output_file"; then
    printf '%s' 'workflow_failed'
  else
    printf '%s' 'success'
  fi
}

kill_descendants() {
  local parent="$1"
  local child
  # pgrep -P is available on macOS. Recurse before killing the parent so
  # grandchildren cannot be reparented and survive the pilot watchdog.
  for child in $(pgrep -P "$parent" 2>/dev/null || true); do
    kill_descendants "$child"
    kill -KILL "$child" 2>/dev/null || true
  done
}

for ATTEMPT in $(seq 1 "$MAX_ATTEMPTS"); do
  STAMP="$(date +%Y%m%d-%H%M%S)"
  OUTPUT_FILE="$LOG_DIR/${STAMP}-attempt${ATTEMPT}.out.log"
  DEBUG_FILE="$LOG_DIR/${STAMP}-attempt${ATTEMPT}.debug.log"
  : > "$OUTPUT_FILE"
  "$PROVIDER_SANDBOX" "$CLAUDE_BIN" -p "$PROMPT_TEXT" --output-format text --debug-file "$DEBUG_FILE" </dev/null >"$OUTPUT_FILE" 2>&1 &
  CLAUDE_PID=$!
  ELAPSED=0
  TIMED_OUT=0
  while kill -0 "$CLAUDE_PID" 2>/dev/null; do
    if [ "$ELAPSED" -ge "$TIMEOUT_SECONDS" ]; then
      kill -TERM "$CLAUDE_PID" 2>/dev/null || true
      sleep 2
      kill_descendants "$CLAUDE_PID"
      pkill -9 -P "$CLAUDE_PID" 2>/dev/null || true
      kill -KILL "$CLAUDE_PID" 2>/dev/null || true
      wait "$CLAUDE_PID" 2>/dev/null || true
      TIMED_OUT=1
      break
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
  done

  if [ "$TIMED_OUT" -eq 1 ]; then
    STATUS='timeout'
    EXIT_CODE=124
  else
    wait "$CLAUDE_PID"
    EXIT_CODE=$?
    if [ "$EXIT_CODE" -eq 0 ]; then
      STATUS="$(classify_workflow_output "$OUTPUT_FILE")"
    else
      STATUS="$(classify_output "$OUTPUT_FILE")"
    fi
  fi

  if [ "$STATUS" = 'success' ]; then
    rm -f "$DEBUG_FILE"
    break
  fi
  # Quota/session exhaustion is not transient for this runner; never spend
  # another attempt on it. A timeout or other failure may be retried only if
  # the caller explicitly configured more than one attempt.
  if [ "$STATUS" = 'provider_unavailable' ] || [ "$STATUS" = 'workflow_detached' ] || [ "$ATTEMPT" -ge "$MAX_ATTEMPTS" ]; then
    break
  fi
done

EVENT_BYTES_AFTER=0
if [ -n "$EVENT_FILE" ] && [ -f "$EVENT_FILE" ]; then
  EVENT_BYTES_AFTER="$(wc -c < "$EVENT_FILE" | tr -d ' ')"
fi
STATUS="${STATUS:-failed}"
SNAPSHOT_STATUS='not_configured'
if [ -n "$USAGE_SNAPSHOT_SCRIPT" ]; then
  SNAPSHOT_ARGS=("$USAGE_SNAPSHOT_SCRIPT")
  if [ -n "$USAGE_SNAPSHOT_FILE" ]; then
    SNAPSHOT_ARGS+=(--output "$USAGE_SNAPSHOT_FILE")
  fi
  if "${SNAPSHOT_ARGS[@]}" >/dev/null 2>&1; then
    SNAPSHOT_STATUS='recorded'
  else
    SNAPSHOT_STATUS='unavailable'
  fi
fi
printf 'status=%s\nexit=%s\noutput_log=%s\nevent_bytes_before=%s\nevent_bytes_after=%s\nusage_snapshot=%s\n' \
  "$STATUS" "${EXIT_CODE:-1}" "$OUTPUT_FILE" "$EVENT_BYTES_BEFORE" "$EVENT_BYTES_AFTER" "$SNAPSHOT_STATUS"

case "$STATUS" in
  success) exit 0 ;;
  provider_unavailable) exit 75 ;;
  timeout) exit 124 ;;
  workflow_detached) exit 125 ;;
  configuration_error) exit 67 ;;
  *) exit 1 ;;
esac
