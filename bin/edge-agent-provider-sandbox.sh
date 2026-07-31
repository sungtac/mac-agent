#!/usr/bin/env bash
# Run one provider CLI under the Edge Agent protected-path sandbox.
# This wrapper never changes the command's arguments or working directory.
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "edge-agent-provider-sandbox: provider command is required" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
PROFILE="${EDGE_AGENT_PROTECTED_PATH_PROFILE:-$SCRIPT_DIR/../config/edge-agent-protected-paths.sb}"
if [ ! -r "$PROFILE" ]; then
  echo "edge-agent-provider-sandbox: sandbox profile is missing or unreadable: $PROFILE" >&2
  exit 2
fi

# Codex has its own workspace-write seatbelt. Nesting it inside macOS
# sandbox-exec prevents Codex's sandbox helper from starting (observed as
# sandbox_apply exit 71 / Operation not permitted). For Codex only, use its
# internal sandbox and refuse the legacy shared workspace, which contains
# Team OS protected roots. This preserves the boundary while allowing safe
# isolated repositories/worktrees to run.
COMMAND_NAME="$(basename -- "$1")"
if [ "$COMMAND_NAME" = "codex" ]; then
  CODEX_CWD="${PWD}"
  for ((i = 1; i < $#; i++)); do
    arg="${!i}"
    next_index=$((i + 1))
    if [ "$arg" = "-C" ] || [ "$arg" = "--cd" ]; then
      CODEX_CWD="${!next_index:-}"
      break
    fi
  done
  CODEX_CWD="$(cd -- "$CODEX_CWD" 2>/dev/null && pwd -P || printf '%s' "$CODEX_CWD")"
  LEGACY_WORKSPACE="/Users/edge_ai/.openclaw/workspace"
  case "$CODEX_CWD" in
    "$LEGACY_WORKSPACE"|"$LEGACY_WORKSPACE"/*)
      echo "edge-agent-provider-sandbox: Codex refuses legacy shared workspace; use an isolated edge workspace/worktree: $CODEX_CWD" >&2
      exit 77
      ;;
    *)
      exec "$@"
      ;;
  esac
fi

exec /usr/bin/sandbox-exec -f "$PROFILE" -- "$@"
