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
if [ "${EDGE_AGENT_PROVIDER_MODE:-}" = "review" ]; then
  PROFILE="${EDGE_AGENT_REVIEW_PROFILE:-$SCRIPT_DIR/../config/code-review-read-only.sb}"
fi
if [ ! -r "$PROFILE" ]; then
  echo "edge-agent-provider-sandbox: sandbox profile is missing or unreadable: $PROFILE" >&2
  exit 2
fi

# Codex has its own workspace-write seatbelt. Nesting it inside macOS
# sandbox-exec prevents Codex's sandbox helper from starting (observed as
# sandbox_apply exit 71 / Operation not permitted). For Codex only, use its
# internal sandbox and refuse the retired OpenClaw paths. This preserves the
# boundary while allowing safe isolated repositories/worktrees to run.
COMMAND_NAME="$(basename -- "$1")"
if [ "$COMMAND_NAME" = "codex" ]; then
  CODEX_CWD="${PWD}"
  for ((i = 1; i <= $#; i++)); do
    arg="${!i}"
    next_index=$((i + 1))
    case "$arg" in
      --) break ;;
      -C|--cd)
        if [ "$next_index" -gt "$#" ]; then
          echo "edge-agent-provider-sandbox: $arg requires a directory" >&2
          exit 64
        fi
        CODEX_CWD="${!next_index}"
        ;;
      -C?*) CODEX_CWD="${arg#-C}" ;;
      --cd=*) CODEX_CWD="${arg#--cd=}" ;;
    esac
  done
  if ! CODEX_CWD="$(python3 - "$CODEX_CWD" <<'PY'
import os
import sys

try:
    print(os.path.realpath(os.path.abspath(sys.argv[1])))
except (IndexError, OSError, UnicodeError):
    raise SystemExit(2)
PY
)"; then
    echo "edge-agent-provider-sandbox: unable to canonicalize Codex working directory" >&2
    exit 64
  fi
  if python3 - "$CODEX_CWD" "$HOME/.openclaw" "$HOME/.edge-agent/retired-openclaw-workspace" <<'PY'
import os
import sys

try:
    target = os.path.realpath(os.path.abspath(sys.argv[1])).casefold()
    roots = [os.path.realpath(os.path.abspath(item)).casefold() for item in sys.argv[2:]]
except (IndexError, OSError, UnicodeError):
    raise SystemExit(2)

for root in roots:
    if target == root or target.startswith(root + os.sep):
        raise SystemExit(0)
raise SystemExit(1)
PY
  then
    echo "edge-agent-provider-sandbox: Codex refuses retired OpenClaw paths; use an isolated edge workspace/worktree: $CODEX_CWD" >&2
    exit 77
  else
    CODEX_PATH_CHECK_RC=$?
    if [ "$CODEX_PATH_CHECK_RC" -ne 1 ]; then
      echo "edge-agent-provider-sandbox: unable to validate Codex working directory" >&2
      exit 64
    fi
    exec "$@"
  fi
fi

exec /usr/bin/sandbox-exec -f "$PROFILE" -- "$@"
