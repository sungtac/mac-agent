#!/usr/bin/env bash
# Host-side Codex execution for calls originating inside Claude's sandbox.
# launchctl submit starts this outside the caller's nested seatbelt.
set -uo pipefail

CWD="$1"
PROMPT_FILE="$2"
OUTPUT_FILE="$3"
STATUS_FILE="$4"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
. "$SCRIPT_DIR/../workflows/lib/provider-bin.sh"

if ! /usr/bin/git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  printf '%s\n' 66 > "$STATUS_FILE"
  exit 0
fi
case "$CWD" in
  /Users/edge_ai/.openclaw/workspace|/Users/edge_ai/.openclaw/workspace/*)
    printf '%s\n' 77 > "$STATUS_FILE"
    exit 0
    ;;
esac

CODEX_BIN="$(find_codex_bin || true)"
if [ -z "$CODEX_BIN" ] || [ ! -x "$CODEX_BIN" ]; then
  printf '%s\n' 69 > "$STATUS_FILE"
  exit 0
fi

CODEX_CWD="$CWD"
CODEX_PROMPT="$(cat "$PROMPT_FILE")"
case "$CODEX_CWD" in
  /private/tmp/*)
    CODEX_CWD="$(printf '%s' "$CODEX_CWD" | sed 's#^/private/tmp/#/tmp/#')"
    CODEX_PROMPT="$(printf '%s' "$CODEX_PROMPT" | sed 's#/private/tmp/#/tmp/#g')"
    ;;
esac

"$CODEX_BIN" exec -s workspace-write -C "$CODEX_CWD" "$CODEX_PROMPT" >"$OUTPUT_FILE" 2>&1
EXIT_CODE=$?
printf '%s\n' "$EXIT_CODE" > "$STATUS_FILE"
exit 0
