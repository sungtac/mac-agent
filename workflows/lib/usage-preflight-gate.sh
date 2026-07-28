#!/usr/bin/env bash
# usage-preflight-gate.sh <claude|codex|dual>
# stdout: "PROCEED" or "SKIP: <reason>" (always exit 0 — decision is in
# stdout, same convention as route-dispatch.sh's "ROUTED-TO: ..." line).
#
# Why this exists (2026-07-28): weekly-report.sh, kakao-morning-briefing.sh,
# and discord-bot.py's claude -p/codex dispatches all fire blind to current
# account usage. On 2026-07-28 alone this caused >=8 independent
# session-limit failures across otherwise-unrelated automations, several
# after 10-30 minutes of a hung/failing run nobody was watching (a scheduled
# launchd job has no human present to notice a hang the way an interactive
# session does). This gate is a cheap pre-check reusing the SAME `coach`
# data usage-advisor.sh/route-dispatch.sh already read — refuse to start if
# the specific window that failure mode depends on is already critically
# low, instead of finding out 26 minutes into a doomed run.
#
# Deliberately NOT built on coach-headroom.sh: that helper collapses
# everything to two bare integers (claude/codex), which is exactly right for
# usage-advisor.sh's "which is bigger" comparison but throws away `level`
# and `reason`, which this gate wants for a human-readable skip message.
# Re-parses `coach --json` directly instead, with the same
# never-string-interpolate-JSON-into-python-source discipline
# coach-headroom.sh documents (piped via stdin here too).
#
# Threshold: SKIP if the relevant window's left_pct < FLOOR_PCT, OR coach's
# own `level` for that provider is "red". Claude is checked on its 5h
# window specifically (not 7d) — that's the window whose exhaustion is what
# actually causes a mid-run failure; the 7d window can be comfortably high
# while 5h is at 0%, exactly as observed live on 2026-07-28 (claude 5h=0%,
# 7d=83%, level=yellow). Codex only has a 7d window
# (coach has no visibility into antigravity at all, same reason
# coach-headroom.sh never reports it — "dual" here means claude+codex only).
#
# Fail-open, not fail-closed: if coach is missing, errors, or returns
# unparseable/incomplete data, this prints PROCEED — an unreachable checker
# must not become a silent kill-switch for every scheduled automation on
# this machine. Same "0/unknown is not the same as confirmed depleted"
# stance coach-headroom.sh already documents.
set -uo pipefail

export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

ACTOR="${1:?usage: usage-preflight-gate.sh <claude|codex|dual>}"
FLOOR_PCT=10

if ! command -v coach >/dev/null 2>&1; then
  echo "PROCEED (coach unavailable — gate skipped, not enforced)"
  exit 0
fi

COACH_OUTPUT="$(coach --json 2>/dev/null)"
if [ -z "$COACH_OUTPUT" ]; then
  echo "PROCEED (coach returned no data — gate skipped, not enforced)"
  exit 0
fi

printf '%s' "$COACH_OUTPUT" | python3 -c "
import json, sys

actor = sys.argv[1]
floor = int(sys.argv[2])

try:
    providers = json.load(sys.stdin)['providers']
except Exception:
    print('PROCEED (coach output unparseable — gate skipped, not enforced)')
    sys.exit(0)

def check(name, window_key):
    p = providers.get(name) or {}
    if not p.get('ok'):
        return None  # provider unreadable — don't block on it (fail open)
    windows = p.get('windows') or {}
    w = windows.get(window_key)
    if not w or w.get('left_pct') is None:
        return None
    pct = w['left_pct']
    level = p.get('level')
    if level == 'red' or pct < floor:
        reason = p.get('reason', '')
        return f'{name} {window_key}창 잔여 {pct}% (level={level}) — {reason}'
    return None

blockers = []
if actor in ('claude', 'dual'):
    r = check('claude', '5h')
    if r:
        blockers.append(r)
if actor in ('codex', 'dual'):
    r = check('codex', '7d')
    if r:
        blockers.append(r)

if blockers:
    print('SKIP: ' + ' / '.join(blockers))
else:
    print('PROCEED')
" "$ACTOR" "$FLOOR_PCT"
