#!/usr/bin/env bash
# Official terminal entrypoint for provider-neutral Edge Agent execution.
# Usage: edge-agent-provider.sh <claude|codex|agy> <prompt-file> [workdir]
set -euo pipefail

PROVIDER="${1:?usage: edge-agent-provider.sh <claude|codex|agy> <prompt-file> [workdir]}"
PROMPT_FILE="${2:?usage: edge-agent-provider.sh <claude|codex|agy> <prompt-file> [workdir]}"
WORKDIR="${3:-$PWD}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
SANDBOX="$ROOT/bin/edge-agent-provider-sandbox.sh"
CHANNEL_RUNTIME="$ROOT/bin/edge_agent_channel_runtime.py"
SESSION_BRIDGE="$ROOT/bin/edge_agent_session_bridge.py"

[ -f "$PROMPT_FILE" ] || { echo "prompt file not found: $PROMPT_FILE" >&2; exit 66; }
[ -d "$WORKDIR" ] || { echo "workdir not found: $WORKDIR" >&2; exit 66; }

SESSION_STARTED=0
if [ -z "${EDGE_AGENT_LOGICAL_SESSION_ID:-}" ]; then
  TASK_ID="${EDGE_AGENT_TASK_ID:-terminal-$(shasum -a 256 "$PROMPT_FILE" | cut -c1-16)}"
  SESSION_PROVIDER="$PROVIDER"
  EDGE_AGENT_LOGICAL_SESSION_ID="$(python3 "$SESSION_BRIDGE" start "$TASK_ID" "$SESSION_PROVIDER" "$WORKDIR")"
  export EDGE_AGENT_LOGICAL_SESSION_ID
  SESSION_STARTED=1
fi

SESSION_CONTEXT=""
if [ -n "${EDGE_AGENT_LOGICAL_SESSION_ID:-}" ]; then
  SESSION_CONTEXT="$(python3 "$SESSION_BRIDGE" context "$EDGE_AGENT_LOGICAL_SESSION_ID")"
fi
PROMPT="$(python3 "$CHANNEL_RUNTIME" render --provider "$PROVIDER" --channel terminal --workdir "$WORKDIR" --request-file "$PROMPT_FILE" --session-context "$SESSION_CONTEXT")"

# Canary path: route terminal provider work through the canonical engine's
# bounded dispatcher.  It is deliberately opt-in until terminal handoff and
# recovery tests pass in the host environment.  The normal path below remains
# unchanged, preserving rollback by unsetting one flag.
if [[ "${EDGE_AGENT_TERMINAL_CORE_DISPATCH:-0}" == "1" || "${EDGE_AGENT_TERMINAL_CORE_DISPATCH:-0}" == "true" ]]; then
  RUNNER="$ROOT/bin/edge-agent-terminal-core-runner.py"
  if [ -f "$RUNNER" ]; then
    PYTHON="${EDGE_AGENT_PYTHON:-/opt/homebrew/bin/python3}"
    RENDERED_PROMPT="$(mktemp "${TMPDIR:-/tmp}/edge-agent-terminal-prompt.XXXXXX")"
    trap 'rm -f "$RENDERED_PROMPT"' EXIT
    printf '%s\n' "$PROMPT" > "$RENDERED_PROMPT"
    set +e
    "$PYTHON" "$RUNNER" "$PROVIDER" "$RENDERED_PROMPT" "$WORKDIR"
    RC=$?
    set -e
    if [ "$SESSION_STARTED" -eq 1 ]; then
      python3 "$SESSION_BRIDGE" finish "$EDGE_AGENT_LOGICAL_SESSION_ID" "$([ "$RC" -eq 0 ] && echo succeeded || echo failed)" || true
    fi
    exit "$RC"
  fi
fi

case "$PROVIDER" in
  claude)
    CLI="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
    [ -x "$CLI" ] || CLI="$(command -v claude)"
    set +e; "$SANDBOX" "$CLI" -p "$PROMPT"; RC=$?; set -e
    ;;
  codex)
    CLI="${CODEX_BIN:-/opt/homebrew/bin/codex}"
    [ -x "$CLI" ] || CLI="$(command -v codex)"
    set +e; "$SANDBOX" "$CLI" exec -s workspace-write -C "$WORKDIR" "$PROMPT"; RC=$?; set -e
    ;;
  agy)
    CLI="${AGY_BIN:-$HOME/.local/bin/agy}"
    [ -x "$CLI" ] || CLI="$(command -v agy)"
    set +e; env -u SSH_CONNECTION -u SSH_TTY -u SSH_CLIENT "$SANDBOX" "$CLI" --print "$PROMPT"; RC=$?; set -e
    ;;
  *)
    echo "unsupported provider: $PROVIDER" >&2
    exit 64
    ;;
esac

if [ "$SESSION_STARTED" -eq 1 ]; then
  python3 "$SESSION_BRIDGE" finish "$EDGE_AGENT_LOGICAL_SESSION_ID" "$([ "$RC" -eq 0 ] && echo succeeded || echo failed)" || true
fi
exit "$RC"
