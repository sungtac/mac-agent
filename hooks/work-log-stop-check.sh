#!/usr/bin/env bash
# Stop hook: when a session ends, decide whether it produced public/work
# output (as opposed to meta work like Claude Code config/skill/hook building,
# or private/personal use) and, if so, background a headless `claude -p`
# call that copies the produced files into the Drive-synced work archive and
# logs a Google Calendar event summarizing what was done.
#
# This hook does NOT block the Stop event and does NOT do the classification
# itself — a shell script can't tell "wrote a report" from "edited this hook
# script", so all judgment is delegated to a headless sub-agent. This script
# is just: (1) a cheap mechanical gate to avoid spawning that sub-agent for
# read-only/trivial sessions, and (2) the dispatcher.
set -uo pipefail

# Recursion guard: the headless sub-call below is itself a `claude` session
# that will fire its OWN Stop event when it finishes. Without this guard this
# script would spawn another sub-call, forever.
if [ "${WORK_LOG_DISPATCHED:-}" = "1" ]; then
  exit 0
fi

CLAUDE_BIN="$HOME/.local/bin/claude"
STATE_DIR="$HOME/.claude/hooks-state/work-log"
DRIVE_ARCHIVE_ROOT="$HOME/Library/CloudStorage/GoogleDrive-sungtac@gmail.com/내 드라이브/업무아카이브"
CALENDAR_ID="sungtac@gmail.com"
# Phase 2.5: pending-job store discord-bot.py reads on a reply (same schema/
# location as weekly-report.sh's Phase 2 v1 — see docs/discord-bot.md).
PENDING_DIR="$HOME/.claude/discord-bot/pending"

# Writes a pending-job file for `discord-notify.sh`'s returned message id, so
# a Discord reply to that message can trigger a retry. No-ops silently if
# discord-notify.sh returned no id (config missing, API call failed, etc.) —
# same "never break the caller" posture as discord-notify.sh itself.
#
# created_at uses naive local-time `datetime.now().isoformat()` (via Python,
# not `date -u ...Z`) to exactly match weekly-report.sh's Phase 2 v1 writer —
# discord-bot.py's handle_pending_reply() compares this against another naive
# `datetime.datetime.now()` call, so a UTC/local or aware/naive mismatch here
# would silently skew the 48h expiry window (or throw on parse, on Python
# versions where fromisoformat rejects a trailing "Z").
write_pending_job() {
  local notify_text="$1"
  local msg_id
  msg_id="$(bash "$HOME/mac-agent/bin/discord-notify.sh" "$notify_text")"
  [ -z "$msg_id" ] && return 0
  mkdir -p "$PENDING_DIR"
  python3 -c "
import json, sys, datetime
job = {
    'type': 'work-log-retry',
    'created_at': datetime.datetime.now().isoformat(),
    'params': {'session_id': sys.argv[1], 'transcript_path': sys.argv[2]},
}
with open(sys.argv[3], 'w') as f:
    json.dump(job, f)
" "$SESSION_ID" "$TRANSCRIPT_PATH" "$PENDING_DIR/${msg_id}.json"
}

INPUT="$(cat)"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)"
TRANSCRIPT_PATH="$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)"

[ -z "$SESSION_ID" ] && exit 0

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  TRANSCRIPT_PATH="$(find "$HOME/.claude/projects" -name "${SESSION_ID}.jsonl" 2>/dev/null | head -1)"
fi
[ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ] && exit 0

# cheap gate: only bother with sessions that actually wrote/edited something
EDIT_WRITE_COUNT="$(grep -o '"name":"\(Edit\|Write\)"' "$TRANSCRIPT_PATH" 2>/dev/null | wc -l | tr -d ' ')"
[ "${EDIT_WRITE_COUNT:-0}" -lt 1 ] && exit 0

mkdir -p "$STATE_DIR"
find "$STATE_DIR" -type f -mtime +14 -delete 2>/dev/null || true

# one dispatch per session (Stop can fire more than once: /clear, /compact, /resume)
MARKER="$STATE_DIR/${SESSION_ID}.dispatched"
[ -f "$MARKER" ] && exit 0
touch "$MARKER"

# Usage pre-flight gate (2026-07-28, same rationale as weekly-report.sh):
# the dispatch below is a headless `claude -p` run just like the others hit
# by today's repeated session-limit failures. Checked AFTER the marker is
# set (not before) — this is a one-shot-per-session dispatch, and a skip
# here is recoverable exactly the same way a timeout/failure already is
# below: write_pending_job() so a Discord reply retries via
# handle_work_log_retry(), which re-runs directly from the saved
# session_id/transcript_path and does not depend on this marker at all.
# Fails open on gate error.
GATE_OUTPUT="$(bash "$HOME/mac-agent/workflows/lib/usage-preflight-gate.sh" claude 2>/dev/null || echo "PROCEED (gate script error — not enforced)")"
if [[ "$GATE_OUTPUT" == SKIP:* ]]; then
  write_pending_job "⏳ work-log 세션 기록을 건너뛰었습니다 — 계정 사용량 부족. 세션 ${SESSION_ID}.
${GATE_OUTPUT#SKIP: }
이 메시지에 답장하면 (사용량 회복 후) 다시 시도합니다." || true
  exit 0
fi

LOGFILE="$STATE_DIR/${SESSION_ID}.log"

PROMPT=$(cat <<PROMPT_EOF
너는 방금 종료된 Claude Code 세션의 트랜스크립트를 읽고 "업무 자동 기록" 여부를 판단하는 에이전트다.

트랜스크립트 파일: ${TRANSCRIPT_PATH}

1. 트랜스크립트를 읽고 이 세션이 아래 중 무엇인지 판단해라:
   (a) 공적/업무성 산출물 세션 — 예: 문서 작성, 보고서, 실제 업무 결과물 생성
   (b) 메타 작업 — Claude Code 자체의 설정/훅/스킬/워크플로우/자동화(이 자동화 시스템 자체 포함)를 만들거나 고치는 세션
   (c) 사적인 개인 용무 (개인 일정, 잡담 등)
   (d) 애매함 / 판단 불가

   (a)가 아니면 아무 도구도 쓰지 말고 "SKIP: <이유>" 한 줄만 출력하고 끝내라. 애매하면 SKIP해라 — 과잉 기록보다 누락이 낫다.

2. (a)로 판단되면:
   - 트랜스크립트에서 Write/Edit 도구로 만들어지거나 수정된 실제 산출물 파일 경로를 찾아라 (Claude Code 자체 설정/훅/스킬 파일은 제외).
   - 그 파일들을 "${DRIVE_ARCHIVE_ROOT}/\$(date +%Y-%m-%d)/" 폴더(없으면 생성)로 복사해라. 원본은 그대로 두고 복사만 해라. (같은 파일명은 같은 목적지 경로로 다시 복사해도 그냥 덮어써질 뿐이니 재실행 걱정 없이 그대로 해라.)
   - **먼저** Google Calendar(calendar_id: ${CALENDAR_ID})에서 오늘 날짜로 description에 정확히 "[세션ID: ${SESSION_ID}]" 문자열이 포함된 기존 이벤트가 있는지 search_events로 찾아봐라.
     - 있으면: 그 이벤트를 update해라(제목/설명을 이번 판단 내용으로 갱신) — 새로 만들지 마라. 이건 재시도 상황이라는 뜻이다.
     - 없으면: 새로 만들어라. 제목은 한 일을 짧게 요약, description에는 2~3문장 요약 + 정리한 파일 목록 + 맨 끝 줄에 반드시 "[세션ID: ${SESSION_ID}]"를 그대로 포함해라(이후 재시도 시 이 문자열로 중복 생성을 막는 유일한 단서다 — 절대 빠뜨리지 마라). 시간은 지금 시각 기준 30분짜리로 잡아라.
   - 마지막에 "LOGGED: <이벤트 제목>" 한 줄을 출력해라.

꼭 필요한 도구 호출만 간결하게 해라.
PROMPT_EOF
)

(
  # Watchdog: `weekly-report.sh` uses this exact same pattern (headless
  # `claude -p`, backgrounded) and was found (2026-07-25) to hang
  # intermittently — connections open, no progress, no exit, forever, with
  # nothing in the log to show for it. This dispatch had zero protection
  # against the same failure mode, and unlike weekly-report (checked once a
  # week) a silent hang here would just accumulate one stuck ~500MB `claude`
  # process per qualifying session with no visibility at all. No automatic
  # retry here (unlike weekly-report) — a retry after a hang that occurred
  # mid-work could plausibly create a duplicate Calendar event or duplicate
  # archived files, which is worse than just missing one session's record.
  TIMEOUT_SECONDS=300
  DEBUG_LOGFILE="$STATE_DIR/${SESSION_ID}.debug.log"
  WORK_LOG_DISPATCHED=1 "$CLAUDE_BIN" -p "$PROMPT" --output-format text \
    --debug-file "$DEBUG_LOGFILE" </dev/null > "$LOGFILE" 2>&1 &
  CLAUDE_PID=$!
  ELAPSED=0
  while kill -0 "$CLAUDE_PID" 2>/dev/null; do
    if [ "$ELAPSED" -ge "$TIMEOUT_SECONDS" ]; then
      kill -TERM "$CLAUDE_PID" 2>/dev/null
      sleep 2
      pkill -9 -P "$CLAUDE_PID" 2>/dev/null
      kill -9 "$CLAUDE_PID" 2>/dev/null
      wait "$CLAUDE_PID" 2>/dev/null
      echo "TIMEOUT after ${TIMEOUT_SECONDS}s — killed. Debug log: ${DEBUG_LOGFILE}. See mac-agent/docs/worklog-hook.md." >> "$LOGFILE"
      write_pending_job "⚠️ work-log 세션 기록 실패(타임아웃). 세션 ${SESSION_ID}. 로그: ${LOGFILE}. 이 메시지에 답장하면 재시도합니다." || true
      exit 1
    fi
    sleep 15
    ELAPSED=$((ELAPSED + 15))
  done
  wait "$CLAUDE_PID"
  EXIT_CODE=$?
  [ "$EXIT_CODE" -eq 0 ] && rm -f "$DEBUG_LOGFILE"
  echo "exit=${EXIT_CODE}" >> "$LOGFILE"
  # Phase 2.5: previously silent on both success and non-timeout failure —
  # a retry caller (or the original dispatch) had no way to know the real
  # outcome. Now notify on a real failure (offer retry), and on a real
  # LOGGED completion (rare-ish, so worth a ping) — but NOT on the common
  # SKIP case (most sessions), since exit=0 covers both "actually logged"
  # and "correctly decided not to log anything", and notifying on every
  # SKIP would spam the channel and defeat the "누락보다 과잉 기록이 낫다"
  # noise-minimizing design this hook already committed to.
  if [ "$EXIT_CODE" -eq 0 ]; then
    if grep -q '^LOGGED:' "$LOGFILE" 2>/dev/null; then
      bash "$HOME/mac-agent/bin/discord-notify.sh" "✅ work-log 세션 기록 완료. 세션 ${SESSION_ID}." >/dev/null || true
    fi
  else
    write_pending_job "⚠️ work-log 세션 기록 실패(exit=${EXIT_CODE}). 세션 ${SESSION_ID}. 로그: ${LOGFILE}. 이 메시지에 답장하면 재시도합니다." || true
  fi
) &
disown

exit 0
