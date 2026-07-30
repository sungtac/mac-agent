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

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=provider-bin.sh
. "$SCRIPT_DIR/provider-bin.sh"

AGY_BIN="${AGY_BIN:-}"
CODEX_BIN="${CODEX_BIN:-}"
[ -n "$AGY_BIN" ] || AGY_BIN="$(find_agy_bin || true)"
[ -n "$CODEX_BIN" ] || CODEX_BIN="$(find_codex_bin || true)"

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
if [ -z "$AGY_BIN" ] || [ ! -x "$AGY_BIN" ]; then
  AGY_OUTPUT=""
else
  AGY_OUTPUT="$(env -u SSH_CONNECTION -u SSH_TTY -u SSH_CLIENT "$AGY_BIN" -p "$(cat "$PROMPT_FILE")" 2>&1)"
fi
# Phrase match alone false-positives when agy's own LEGITIMATE answer
# happens to discuss rate limiting as its actual subject (e.g. the prompt
# asked how to implement rate limiting) — a real depletion/error message
# from the CLI is short (an error line or two), while a substantive answer
# that merely mentions the phrase in passing is typically much longer.
# Requiring the phrase match AND a short output (2026-07-29 fix) doesn't
# eliminate false positives (a short reply that happens to define "rate
# limiting" in one sentence could still trip it) but meaningfully narrows
# the window versus matching on content alone, which used to misroute on
# any mention anywhere in an arbitrarily long real answer.
AGY_LOOKS_DEPLETED=0
if [ -z "$AGY_OUTPUT" ]; then
  AGY_LOOKS_DEPLETED=1
elif [ "${#AGY_OUTPUT}" -lt 200 ] && printf '%s' "$AGY_OUTPUT" | grep -qi 'rate.limit\|rate limit\|quota exceeded\|HTTP 429\|"code": *429'; then
  AGY_LOOKS_DEPLETED=1
fi
if [ "$AGY_LOOKS_DEPLETED" -eq 0 ]; then
  echo "ROUTED-TO: antigravity"
  printf '%s\n' "$AGY_OUTPUT"
  exit 0
fi

read -r CLAUDE_PCT CODEX_PCT < <(bash "$(dirname "$0")/coach-headroom.sh")

echo "ROUTED-TO: codex (antigravity unavailable/depleted; headroom at fallback time — claude:${CLAUDE_PCT:-0}% codex:${CODEX_PCT:-0}%)"
if [ -z "$CODEX_BIN" ] || [ ! -x "$CODEX_BIN" ]; then
  echo "route-dispatch: codex 실행파일을 찾을 수 없음(CODEX_BIN override 또는 Homebrew 경로 확인)" >&2
  exit 1
fi
"$CODEX_BIN" exec --skip-git-repo-check "$(cat "$PROMPT_FILE")" 2>&1
