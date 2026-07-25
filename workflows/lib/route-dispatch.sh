#!/usr/bin/env bash
# Usage-aware router for ad-hoc SIMPLE auxiliary work during live sessions —
# NOT for verify-task-v2's principled role assignments (those stay fixed
# regardless of usage; changing them would break the author≠grader design).
# This implements Rules 2-3 of the 4-point policy in docs/usage-routing.md
# ("Rule B" — antigravity gets the explicit trigger of being invoked here,
# for work already judged delegable-and-simple; falls back to codex if
# antigravity looks depleted). Rule 1 (codex-capable work: compare codex vs
# claude headroom) is usage-advisor.sh, not this script — that decision can
# route work to "claude" (i.e. the live orchestrator just does it itself),
# which no script can cause to happen, so it stays a live judgment call
# informed by a deterministic recommendation instead of being dispatched
# from here.
#
# Usage: route-dispatch.sh <prompt-file>
# stdout: "ROUTED-TO: <agent>" on its own first line, then the raw response.
set -uo pipefail

PROMPT_FILE="${1:?usage: route-dispatch.sh <prompt-file>}"

if [ ! -f "$PROMPT_FILE" ]; then
  echo "ROUTED-TO: none"
  echo "route-dispatch: prompt file not found: $PROMPT_FILE" >&2
  exit 1
fi

# Deliberately NOT built on score-dispatch.sh here: that script requires a
# JSON-shaped response and wraps anything else in a dealbreaker:true failure
# envelope (correct for scoring prompts, wrong for general free-text
# questions) — confirmed by testing this router with a plain-text prompt,
# where a perfectly good antigravity answer got misread as "depleted" and
# silently fell back to codex every time. This calls agy directly and only
# treats an actually-empty response or literal rate-limit/quota wording as
# a depletion signal.
AGY_OUTPUT="$(env -u SSH_CONNECTION -u SSH_TTY -u SSH_CLIENT /Users/edge_ai/.local/bin/agy -p "$(cat "$PROMPT_FILE")" 2>&1)"
if [ -n "$AGY_OUTPUT" ] && ! printf '%s' "$AGY_OUTPUT" | grep -qi 'rate.limit\|rate limit\|quota exceeded\|HTTP 429\|"code": *429'; then
  echo "ROUTED-TO: antigravity"
  printf '%s\n' "$AGY_OUTPUT"
  exit 0
fi

read -r CLAUDE_PCT CODEX_PCT < <(bash "$(dirname "$0")/coach-headroom.sh")

echo "ROUTED-TO: codex (antigravity unavailable/depleted; headroom at fallback time — claude:${CLAUDE_PCT:-0}% codex:${CODEX_PCT:-0}%)"
/opt/homebrew/bin/codex exec --skip-git-repo-check "$(cat "$PROMPT_FILE")" 2>&1
