#!/usr/bin/env bash
# Deterministic recommendation for "codex-capable" work: compare Claude's
# and Codex's remaining usage (via coach-headroom.sh) and print which one
# has more headroom. This is Rule 1 of docs/usage-routing.md's 4-point
# policy — the ONE point of that policy that genuinely cannot be code-level
# ENFORCED (there is no script that can make "the live orchestrating Claude
# session" do a task instead of dispatching it — "claude does it" just means
# the orchestrator proceeds normally, no process gets spawned). What CAN be
# enforced is that the comparison itself is computed here, deterministically,
# instead of left to the orchestrator's own possibly-convenient guess.
#
# Usage: usage-advisor.sh
# stdout: "PREFER: codex (claude:NN% codex:MM%)" or "PREFER: claude (...)"
#
# Tie-break and failure handling: on a tie, or if Claude's headroom can't be
# read (coach-headroom.sh returns 0 for it), prefer codex — codex's usage
# actually IS visible and rarely tight (96%+ observed historically), so
# routing away from an unverifiable/uncertain Claude reading is the safer
# default, not a coin flip.
set -uo pipefail

# $(dirname "$0")-relative, not a hardcoded absolute path (2026-07-29 fix,
# matches route-dispatch.sh's own sibling-file pattern in this same
# directory) — the old hardcoded "/Users/edge_ai/mac-agent/..." path breaks
# silently if this repo is ever cloned somewhere else: `bash` fails to find
# the file, `read` gets nothing, and the `${CLAUDE_PCT:-0}`/`${CODEX_PCT:-0}`
# fallbacks below quietly turn that into "always prefer codex" with no error
# at all — the worst kind of failure (wrong answer, no signal).
COACH_HEADROOM="$(dirname "$0")/coach-headroom.sh"

read -r CLAUDE_PCT CODEX_PCT < <(bash "$COACH_HEADROOM")
CLAUDE_PCT="${CLAUDE_PCT:-0}"
CODEX_PCT="${CODEX_PCT:-0}"

if [ "$CLAUDE_PCT" -gt "$CODEX_PCT" ]; then
  echo "PREFER: claude (claude:${CLAUDE_PCT}% codex:${CODEX_PCT}%)"
else
  echo "PREFER: codex (claude:${CLAUDE_PCT}% codex:${CODEX_PCT}%)"
fi
