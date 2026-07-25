#!/usr/bin/env bash
# Tiny shared helper: prints "<claude_pct> <codex_pct>" (space-separated
# integers, remaining-usage percentage per `coach`) on a single line.
# Extracted out of route-dispatch.sh so usage-advisor.sh doesn't duplicate
# the same coach-JSON-parsing logic. Antigravity is deliberately not
# reported here — `coach` has no visibility into it ("데이터 부족"), which
# is exactly why route-dispatch.sh/usage-advisor.sh treat antigravity
# optimistically (try it, treat failure as the depletion signal) instead of
# reading a number for it.
#
# On any failure (coach missing, non-JSON output, missing fields) prints
# "0 0" — callers should treat 0 as "unknown/assume worst," not "confirmed
# zero remaining."
set -uo pipefail

# Fix PATH for the whole script, not just the coach invocation below — a
# `command -v coach` check using the caller's own (possibly stripped) PATH
# was found to report "not found" even when coach is installed, since the
# existence check and the actual call used different PATHs.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

if ! command -v coach >/dev/null 2>&1; then
  echo "0 0"
  exit 0
fi

# Piped via stdin, never interpolated into the Python source — JSON
# containing quotes/backslashes would otherwise break a naive `'''$VAR'''`
# embed.
coach --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)['providers']
    claude_pct = int(d['claude']['windows'].get('5h', d['claude']['windows'].get('7d', {})).get('left_pct', 0))
    codex_pct = int(d['codex']['windows'].get('7d', {}).get('left_pct', 0))
    print(claude_pct, codex_pct)
except Exception:
    print(0, 0)
" 2>/dev/null || echo "0 0"
