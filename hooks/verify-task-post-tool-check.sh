#!/usr/bin/env bash
# Retired PostToolUse(Workflow) observer.
#
# The canonical host orchestrator writes verify-task session state directly.
# The removed JavaScript Workflow adapter must never unlock the edit gate, so
# this compatibility hook intentionally performs no state transition.
set -uo pipefail
cat >/dev/null
exit 0
