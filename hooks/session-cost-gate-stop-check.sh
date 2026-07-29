#!/usr/bin/env bash
# Stop hook: nags (once per session) when the CURRENT context size looks
# like it's approaching the point where compacting/starting a new session
# pays off — 실밸개발자 Claude Code 강의 2편(2026-07-29 검토·반영), same
# source as the CLAUDE.md "don't edit mid-session" rule and the
# token-cost-dashboard skill.
#
# "Current context size" here means the most recent assistant turn's
# input_tokens + cache_read_input_tokens + cache_creation_input_tokens —
# i.e. how much was actually in the prompt for that turn. This is NOT the
# same thing as cumulative session cost (that's what the token-cost-dashboard
# skill reports, retrospectively, across many sessions) — summing usage
# across all turns in one session would double-count the same growing
# context repeatedly, since each turn's tokens mostly overlap with the
# previous turn's already-cached content.
#
# Threshold 180,000 is a deliberate margin below the 200K figure the source
# video suggested for auto-compact — gives the user a heads-up before actually
# hitting that mark. `model=="<synthetic>"` turns (rate-limit/error, zero real
# usage) are skipped, matching the same convention used in
# token-cost-dashboard's analyze_sessions.py.
#
# Same nag-once-per-session marker-file pattern as verify-task-stop-check.sh
# and usage-routing-check.sh in this repo — blocks (not silent-logs) exactly
# once via {decision:"block", reason, systemMessage}, same JSON contract as
# those two scripts.
set -uo pipefail

INPUT="$(cat)"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)"
TRANSCRIPT_PATH="$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)"

[ -z "$SESSION_ID" ] && exit 0

STATE_DIR="$HOME/.claude/hooks-state/session-cost-gate-nag"
mkdir -p "$STATE_DIR"
NAG_MARKER="$STATE_DIR/${SESSION_ID}.nagged"

find "$STATE_DIR" -type f -mtime +7 -delete 2>/dev/null || true

[ -f "$NAG_MARKER" ] && exit 0

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  TRANSCRIPT_PATH="$(find "$HOME/.claude/projects" -name "${SESSION_ID}.jsonl" 2>/dev/null | head -1)"
fi
[ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ] && exit 0

THRESHOLD=180000

RESULT="$(python3 -c "
import json, sys

path, threshold = sys.argv[1], int(sys.argv[2])
last_ctx = None
last_ratio = 0.0

with open(path, encoding='utf-8', errors='replace') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get('type') != 'assistant':
            continue
        message = obj.get('message') or {}
        if message.get('model') == '<synthetic>':
            continue
        usage = message.get('usage') or {}
        if not usage:
            continue
        inp = usage.get('input_tokens', 0) or 0
        cread = usage.get('cache_read_input_tokens', 0) or 0
        ccreate = usage.get('cache_creation_input_tokens', 0) or 0
        last_ctx = inp + cread + ccreate
        last_ratio = (cread / last_ctx) if last_ctx else 0.0

if last_ctx is None:
    print('NONE 0 0')
elif last_ctx >= threshold:
    print(f'OVER {last_ctx} {last_ratio:.2f}')
else:
    print(f'UNDER {last_ctx} {last_ratio:.2f}')
" "$TRANSCRIPT_PATH" "$THRESHOLD" 2>/dev/null)"

STATUS="$(printf '%s' "$RESULT" | awk '{print $1}')"
[ "$STATUS" != "OVER" ] && exit 0

CTX="$(printf '%s' "$RESULT" | awk '{print $2}')"
RATIO="$(printf '%s' "$RESULT" | awk '{print $3}')"

touch "$NAG_MARKER"

jq -n --arg ctx "$CTX" --arg ratio "$RATIO" --arg threshold "$THRESHOLD" '{
  decision: "block",
  reason: ("이 세션의 현재 컨텍스트가 약 " + $ctx + " 토큰입니다(기준 " + $threshold + "). 이번 턴 캐시적중률은 " + $ratio + "입니다. 계속 이어가는 것보다 새 세션을 여는 걸 고려하세요 — 컨텍스트가 클수록 캐시 미스 한 번의 재계산 비용도 커집니다. CLAUDE.md를 고치거나 모델을 바꿀 계획이 있다면 지금(세션 경계)이 그 타이밍입니다."),
  systemMessage: "세션 컨텍스트 임계값 도달 — 새 세션 시작을 고려하세요 (이번 세션 1회 안내)."
}'
exit 0
