#!/usr/bin/env bash
# Deterministic test entrypoint for the active, non-Discord Edge Agent scope.
set -uo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT/bin:$ROOT${PYTHONPATH:+:$PYTHONPATH}"
failed=0

# A full suite touches process-wide state and launchd-shaped files.  Serialize
# competing full-suite invocations so a developer or CI job cannot turn a
# deterministic routing test into a shared-fixture race.
LOCK_PATH="${TMPDIR:-/tmp}/edge-agent-active-tests-${USER:-unknown}.lock"
if [ "${EDGE_AGENT_TEST_LOCK_HELD:-0}" != "1" ]; then
  exec env EDGE_AGENT_TEST_LOCK_HELD=1 python3 "$ROOT/bin/run-with-active-test-lock.py" \
    --lock "$LOCK_PATH" -- "$ROOT/bin/run-active-tests.sh" "$@"
fi

for test_file in "$ROOT"/tests/test_*.py; do
  python3 "$test_file" || failed=1
done

for test_file in "$ROOT"/skills/*/tests/test_*.py; do
  python3 "$test_file" || failed=1
done

node --test "$ROOT"/tests/*.test.js || failed=1

exit "$failed"
