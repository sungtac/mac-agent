#!/usr/bin/env bash
# Runs every Thursday 18:00 via launchd (~/Library/LaunchAgents/com.macagent.weekly-report.plist)
# to compile the week's Google Calendar work log into a report, saved locally
# into the Drive-synced 업무아카이브 folder (auto-uploads to sungtac@gmail.com's Drive),
# and also logs a same-content Calendar event on that week's Friday so the
# report is visible directly on the calendar, not just buried in a Drive
# folder. Moved from Friday 18:07 to Thursday 18:00 on 2026-07-26 — the date
# math below (Mon..Fri range derived from ISO weekday offset, not "today")
# already handles firing a day early correctly, no changes needed there.
# Known trade-off of firing Thursday evening instead of Friday: whatever work
# happens on Friday itself (the report's own last day) hasn't happened yet at
# generation time, so it can't be in "이번 주 한 일" — accepted knowingly, not
# a bug.
#
# Deliberately avoids the Google Drive MCP connector entirely — that connector
# is authenticated as a third party's account (sungwan777@gmail.com), not the
# user's own, and is blocked at the settings.json permission level (see
# permissions.deny in ~/.claude/settings.json). This script only uses the
# Calendar MCP (correctly authenticated as sungtac@gmail.com) plus local
# filesystem writes into the already-sungtac-owned synced folder.
set -uo pipefail

CLAUDE_BIN="$HOME/.local/bin/claude"
DRIVE_ROOT="$HOME/Library/CloudStorage/GoogleDrive-sungtac@gmail.com/내 드라이브"
ARCHIVE_ROOT="$DRIVE_ROOT/업무아카이브"
ARCHIVE_ROOT_TOP="$DRIVE_ROOT/주간보고서"
CALENDAR_ID="sungtac@gmail.com"
STATE_DIR="$HOME/.claude/hooks-state/weekly-report"
mkdir -p "$STATE_DIR"

TODAY="$(date +%Y-%m-%d)"
LOGFILE="$STATE_DIR/${TODAY}.log"

# Compute the Monday..Friday range of the current week regardless of which
# day this actually fires on (launchd can run a missed job late on wake).
WEEKDAY="$(date +%u)"   # 1=Mon .. 7=Sun
DAYS_SINCE_MON=$((WEEKDAY - 1))
MON="$(date -v-${DAYS_SINCE_MON}d +%Y-%m-%d)"
FRI="$(date -v-${DAYS_SINCE_MON}d -v+4d +%Y-%m-%d)"
MON_Y="$(date -v-${DAYS_SINCE_MON}d +%Y)"; MON_M=$((10#$(date -v-${DAYS_SINCE_MON}d +%m))); MON_D=$((10#$(date -v-${DAYS_SINCE_MON}d +%d)))
FRI_Y="$(date -v-${DAYS_SINCE_MON}d -v+4d +%Y)"; FRI_M=$((10#$(date -v-${DAYS_SINCE_MON}d -v+4d +%m))); FRI_D=$((10#$(date -v-${DAYS_SINCE_MON}d -v+4d +%d)))

FOLDER_RANGE="${MON}~${FRI}"
TITLE="[주간 업무 보고] ${MON_Y}년 ${MON_M}월 ${MON_D}일 ~ ${FRI_Y}년 ${FRI_M}월 ${FRI_D}일"
REPORT_PATH="${ARCHIVE_ROOT_TOP}/${FOLDER_RANGE}/${TITLE}.md"

PROMPT=$(cat <<PROMPT_EOF
매주 목요일 18시에 실행되는 주간 업무 보고서 작성 작업입니다. 이번 주 범위: ${MON} ~ ${FRI} (오늘은 아직 목요일이라 금요일 하루치는 반영이 안 됩니다 — 정상입니다).

절차:
1. Google Calendar MCP 도구로 calendar_id \`${CALENDAR_ID}\`에서 ${MON}부터 ${FRI}까지의 모든 이벤트를 가져오세요. '대한민국의 휴일' 같은 공휴일 캘린더 이벤트는 제외하세요.
2. Bash/Read 도구로 "${ARCHIVE_ROOT}" 폴더 안에서 이번 주 날짜(${MON}~${FRI}) 하위 폴더가 있는지 확인해서(ls 등), 캘린더 기록과 교차 확인하고 파일 목록에 참고하세요.
3. 아래 두 섹션으로 보고서를 작성하세요:
   - '이번 주 한 일': 이번 주 캘린더 이벤트들을 날짜별로 종합 정리. 이벤트가 하나도 없으면 '이번 주 기록된 업무 없음'이라고 명시하세요.
   - '다음 주 할 일 (제안)': 이번 주 기록에서 보이는 미완료·후속 작업, 그리고 다음 주에 이미 캘린더에 잡혀 있는 일정이 있다면 참고해서 초안을 제안하세요. 이 섹션 맨 위에 반드시 '※ 아래 항목은 에이전트가 제안하는 초안이며 실제 계획이 아닙니다. 검토 후 직접 수정해주세요.'라는 문구를 넣으세요.
4. Write 도구로 이 보고서를 마크다운 파일로 저장하세요. 경로: "${REPORT_PATH}" (폴더가 없으면 Bash로 먼저 mkdir -p 하세요). 파일 맨 위에 "# ${TITLE}" 제목을 넣으세요.
5. Google Calendar MCP 도구로 calendar_id \`${CALENDAR_ID}\`에 이번 주 금요일(${FRI}) 09:00~09:30 이벤트를 하나 새로 생성하세요. 제목은 "${TITLE}", description에는 3번에서 쓴 두 섹션 내용을 그대로(요약하지 말고) 넣고 맨 끝 줄에 "전체 파일: ${REPORT_PATH}"를 추가하세요.
6. 마지막에 저장한 파일의 절대 경로와 생성한 캘린더 이벤트 ID를 각각 한 줄로 출력하세요.

참고: 이 세션에는 Google Drive MCP 도구가 연결되어 있지 않을 수 있습니다 — 의도된 설정이니 신경쓰지 말고 로컬 파일시스템(Bash/Write)으로만 저장하세요.
PROMPT_EOF
)

# Watchdog + retry: a launchd-triggered `claude -p` run has been observed to
# hang intermittently (confirmed 2026-07-25 via live repro: ~16 TCP
# connections to api.anthropic.com sit ESTABLISHED with empty send/recv
# queues and near-zero CPU — a stall inside the Bun-based CLI's HTTP
# connection pool, not a network/DNS/proxy/TCC/keychain issue; those were all
# ruled out). It is NOT deterministic — the identical launchd-triggered
# command hung 3x in a row earlier the same day, then succeeded on the very
# next manual retry. So instead of chasing the opaque compiled binary further,
# retry a few times per firing; on any hang, dump `--debug-file` output for
# that attempt so a future investigation has real internals to look at
# instead of guessing again.
TIMEOUT_SECONDS=240
MAX_ATTEMPTS=3
SUCCESS=0
for ATTEMPT in $(seq 1 "$MAX_ATTEMPTS"); do
  DEBUG_LOGFILE="$STATE_DIR/${TODAY}-attempt${ATTEMPT}-debug.log"
  echo "--- attempt ${ATTEMPT}/${MAX_ATTEMPTS} ---" >> "$LOGFILE"
  WORK_LOG_DISPATCHED=1 "$CLAUDE_BIN" -p "$PROMPT" --output-format text \
    --debug-file "$DEBUG_LOGFILE" </dev/null >> "$LOGFILE" 2>&1 &
  CLAUDE_PID=$!
  ELAPSED=0
  TIMED_OUT=0
  while kill -0 "$CLAUDE_PID" 2>/dev/null; do
    if [ "$ELAPSED" -ge "$TIMEOUT_SECONDS" ]; then
      # SIGTERM first (grace period so --debug-file gets a chance to flush,
      # and so claude can ask any MCP-server child processes it has already
      # spawned to exit) before SIGKILL. Then explicitly reap any leftover
      # children by PPID — the background `claude` process shares this
      # script's process group (no `set -m` / job control in a non-interactive
      # script), so `kill -9 "$CLAUDE_PID"` alone only kills claude itself and
      # would silently orphan any MCP-server subprocess it had already spawned.
      kill -TERM "$CLAUDE_PID" 2>/dev/null
      sleep 2
      pkill -9 -P "$CLAUDE_PID" 2>/dev/null
      kill -9 "$CLAUDE_PID" 2>/dev/null
      wait "$CLAUDE_PID" 2>/dev/null
      TIMED_OUT=1
      break
    fi
    sleep 15
    ELAPSED=$((ELAPSED + 15))
  done
  if [ "$TIMED_OUT" -eq 1 ]; then
    echo "attempt ${ATTEMPT}/${MAX_ATTEMPTS}: TIMEOUT after ${TIMEOUT_SECONDS}s — killed. Debug log: ${DEBUG_LOGFILE}. See mac-agent/docs/weekly-report.md for the known intermittent launchd-hang issue." >> "$LOGFILE"
    sleep 5
    continue
  fi
  wait "$CLAUDE_PID"
  EXIT_CODE=$?
  echo "attempt ${ATTEMPT}/${MAX_ATTEMPTS}: exit=${EXIT_CODE}" >> "$LOGFILE"
  if [ "$EXIT_CODE" -eq 0 ]; then
    SUCCESS=1
    rm -f "$DEBUG_LOGFILE"
    break
  fi
done

if [ "$SUCCESS" -ne 1 ]; then
  echo "all ${MAX_ATTEMPTS} attempts failed." >> "$LOGFILE"
  exit 1
fi
