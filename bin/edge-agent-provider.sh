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
CAPABILITY_RESOLVER="$ROOT/bin/edge_agent_capability_registry.py"

[ -f "$PROMPT_FILE" ] || { echo "prompt file not found: $PROMPT_FILE" >&2; exit 66; }
[ -d "$WORKDIR" ] || { echo "workdir not found: $WORKDIR" >&2; exit 66; }

PROMPT="$(python3 "$CAPABILITY_RESOLVER" --prompt "$(cat "$PROMPT_FILE")"; printf '\n\n[터미널 작업 요청]\n'; cat "$PROMPT_FILE")"

case "$PROVIDER" in
  claude)
    CLI="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
    [ -x "$CLI" ] || CLI="$(command -v claude)"
    exec "$SANDBOX" "$CLI" -p "$PROMPT"
    ;;
  codex)
    CLI="${CODEX_BIN:-/opt/homebrew/bin/codex}"
    [ -x "$CLI" ] || CLI="$(command -v codex)"
    exec "$SANDBOX" "$CLI" exec -s workspace-write -C "$WORKDIR" "$PROMPT"
    ;;
  agy)
    CLI="${AGY_BIN:-$HOME/.local/bin/agy}"
    [ -x "$CLI" ] || CLI="$(command -v agy)"
    exec env -u SSH_CONNECTION -u SSH_TTY -u SSH_CLIENT "$SANDBOX" "$CLI" --print "$PROMPT"
    ;;
  *)
    echo "unsupported provider: $PROVIDER" >&2
    exit 64
    ;;
esac
