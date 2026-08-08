#!/usr/bin/env bash
# Run the active Edge Agent suite with test-only state isolated from services.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ISOLATED_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/edge-agent-test-state.XXXXXX")"
cleanup() {
  rm -rf -- "$ISOLATED_ROOT"
}
trap cleanup EXIT

export EDGE_AGENT_TEST_LOCK_ROOT="$ISOLATED_ROOT"
exec "$ROOT/bin/run-active-tests.sh" "$@"
