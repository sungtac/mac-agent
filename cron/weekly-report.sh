#!/usr/bin/env bash
# Runs every Thursday 18:00 via launchd (~/Library/LaunchAgents/com.macagent.weekly-report.plist)
# to compile the week's Google Calendar work log into a report, saved locally
# into the Drive-synced 주간보고서 folder (auto-uploads to sungtac@gmail.com's Drive),
# and also logs a same-content Calendar event on that week's Friday so the
# report is visible directly on the calendar, not just buried in a Drive
# folder. Moved from Friday 18:07 to Thursday 18:00 on 2026-07-26 — the date
# math below (Mon..Fri range derived from ISO weekday offset, not "today")
# already handles firing a day early correctly, no changes needed there.
# 2026-07-28: the Calendar query below always fetched through Friday (the
# date range was never the bug), but the prompt told the agent Friday
# "isn't reflected -- that's normal," so it saw Friday events in the fetched
# range and still omitted them. Fixed per user request -- the user reports
# every Friday, so any Friday event already on the calendar at Thursday
# 18:00 generation time (e.g. a meeting scheduled in advance) must be
# included in "이번 주 한 일", not treated as "hasn't happened yet." What
# genuinely can't be captured: work-log-hook entries for sessions that
# haven't run yet as of Thursday 18:00 -- a real time-ordering limit, not
# something a prompt fix can close.
#
# Uses the Calendar MCP (authenticated as sungtac@gmail.com) plus local
# filesystem writes into the already-sungtac-owned synced folder. Historical
# note: until 2026-07-28 the Google Drive MCP connector was misauthenticated
# as a third party's account (sungwan777@gmail.com) and hard-blocked via
# permissions.deny in ~/.claude/settings.json — that has since been fixed
# (Drive connector reconnected to sungtac@gmail.com, deny entries removed).
# This script still deliberately skips Drive MCP and uses local filesystem
# writes, since that path is already tested and working; switching to Drive
# MCP would be a deliberate follow-up change, not required by the fix above.
set -uo pipefail

CLAUDE_BIN="$HOME/.local/bin/claude"
DRIVE_ROOT="$HOME/Library/CloudStorage/GoogleDrive-sungtac@gmail.com/내 드라이브"
ARCHIVE_ROOT_TOP="$DRIVE_ROOT/주간보고서"
CALENDAR_ID="sungtac@gmail.com"
STATE_DIR="$HOME/.claude/hooks-state/weekly-report"
mkdir -p "$STATE_DIR"

TODAY="$(date +%Y-%m-%d)"
LOGFILE="$STATE_DIR/${TODAY}.log"

# Usage pre-flight gate (2026-07-28): this script's whole job is a headless
# `claude -p` run, and firing it while the account's short window is
# already near-empty is exactly what caused repeated silent/hung failures
# on 2026-07-28. Check BEFORE taking the mutex lock below, not after — no
# point holding the lock for a run we're not going to attempt. Exit code 4
# = skipped for low usage headroom, distinct from 0/1/3 so discord-bot.py
# can report it accurately instead of miscasting it as success or failure.
GATE_OUTPUT="$(bash "$HOME/mac-agent/workflows/lib/usage-preflight-gate.sh" claude 2>/dev/null || echo "PROCEED (gate script error — not enforced)")"
if [[ "$GATE_OUTPUT" == SKIP:* ]]; then
  echo "usage gate: ${GATE_OUTPUT} — skipping this run." >> "$LOGFILE"
  MSG_ID="$(bash "$HOME/mac-agent/bin/discord-notify.sh" "⏳ 주간보고서 실행을 건너뛰었습니다 — 계정 사용량 부족.
${GATE_OUTPUT#SKIP: }
이 메시지에 답장하면 (사용량 회복 후) 다시 시도합니다." || true)"
  if [ -n "$MSG_ID" ]; then
    PENDING_DIR="$HOME/.claude/discord-bot/pending"
    mkdir -p "$PENDING_DIR"
    python3 -c "import json,sys,datetime
json.dump({'type': 'weekly-report-retry', 'created_at': datetime.datetime.now().isoformat(), 'params': {}},
           open(sys.argv[1], 'w'))" "$PENDING_DIR/${MSG_ID}.json"
  fi
  echo "SKIPPED_LOW_USAGE"
  exit 4
fi

# Mutex: launchd's Thursday schedule, `!주간보고서`, and Phase 2's reply-retry
# can all end up invoking this script around the same time. Without a lock,
# two concurrent runs can each pass the Calendar search-first check before
# either has created/updated the event, defeating the idempotency guard added
# below and creating a duplicate. Exit code 3 = skipped (another run is
# already in progress) — distinct from 0 (success) and 1 (failed after
# retries) so discord-bot.py can report it accurately instead of showing a
# skip as either success or failure.
LOCK_FILE="$STATE_DIR/.lock"
LOCK_MAX_AGE_SECONDS=1800   # generous above the ~13min worst-case full run
if [ -f "$LOCK_FILE" ]; then
  LOCK_PID="$(head -1 "$LOCK_FILE" 2>/dev/null)"
  LOCK_MTIME="$(stat -f %m "$LOCK_FILE" 2>/dev/null)"
  if [ -z "$LOCK_MTIME" ]; then
    # stat failed even though `-f` above confirmed the file exists
    # (transient fs hiccup) — treat as "just created" (age 0), not "ancient"
    # (2026-07-29 fix). The old `|| echo 0` fallback computed LOCK_AGE as
    # `now - epoch 0` — a huge number that always looks maximally stale
    # regardless of the lock's real age, defeating this lock exactly when a
    # transient stat failure coincides with a real concurrent run (the one
    # moment it exists to protect). Falling back to "fresh" is safe because
    # the `kill -0 "$LOCK_PID"` check right below is the real staleness
    # signal: a genuinely dead owner still gets correctly taken over either
    # way, so only a fresh live lock is now correctly preserved instead of
    # being defeated by an unrelated stat failure.
    LOCK_MTIME="$(date +%s)"
  fi
  LOCK_AGE=$(( $(date +%s) - LOCK_MTIME ))
  if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null && [ "$LOCK_AGE" -lt "$LOCK_MAX_AGE_SECONDS" ]; then
    echo "already running (pid ${LOCK_PID}, lock age ${LOCK_AGE}s) — skipping to avoid duplicate Calendar events." >> "$LOGFILE"
    echo "SKIPPED_ALREADY_RUNNING (pid ${LOCK_PID})"
    exit 3
  fi
  echo "stale lock (pid ${LOCK_PID:-unknown}, age ${LOCK_AGE}s) — taking over." >> "$LOGFILE"
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

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
매주 목요일 18시에 실행되는 주간 업무 보고서 작성 작업입니다. 이번 주 범위: ${MON} ~ ${FRI}. 사용자가 실제 보고를 금요일에 하기 때문에, 오늘이 아직 목요일이라도 ${FRI}(금요일)에 이미 캘린더에 등록된 일정이 있다면 절대 빠뜨리지 말고 반드시 '이번 주 한 일'에 포함하세요 — "아직 안 지나간 날이니 제외"하는 판단을 하면 안 됩니다. (다만 work-log 훅 특성상 금요일 당일 세션에서 자동 기록되는 이벤트는 이 실행 시점엔 아직 없을 수 있음 — 그건 어쩔 수 없는 시간 순서 문제지, 의도적 제외가 아닙니다.)

절차:
1. Google Calendar MCP 도구로 calendar_id \`${CALENDAR_ID}\`에서 ${MON}부터 ${FRI}까지의 모든 이벤트를 가져오세요(금요일 포함, 절대 누락 금지). '대한민국의 휴일' 같은 공휴일 캘린더 이벤트는 제외하세요.
2. 아래 두 섹션으로 보고서를 작성하세요:
   - '이번 주 한 일': 이번 주 캘린더 이벤트를(금요일 포함) 날짜순 flat bullet 목록으로 나열하세요. 이 섹션은 실제 확정된 사실이므로 bullet 마커는 반드시 "○ "를 쓰세요("- "는 쓰지 마세요 — "-"는 아래 부가설명 줄 전용입니다). '### 날짜' 같은 날짜별 소제목은 쓰지 마세요. 날짜가 바뀌는 첫 항목에만 줄 끝에 "(MM.DD.)" 형식(두 자리 월.두 자리 일.)으로 날짜를 붙이고, 같은 날짜의 나머지 항목에는 날짜를 반복해서 붙이지 마세요. 예:
     ○ [TIPS] 접수 완료(07.27.)
     ○ [AI확산] 통합 시스템 견적 변경 및 공유(07.28.)
     ○ [AI확산] 수산물분과 주요성과 및 현장적용 결과보고서 수정 및 공유 완료
     (위처럼 07.28.도 그날 첫 항목에만 붙고 나머지 줄엔 날짜 없음)
     이벤트가 없는 날은 별도로 언급하지 말고 그냥 건너뛰세요. 이번 주에 이벤트가 하나도 없으면 '이번 주 기록된 업무 없음'이라고 명시하세요.
     캘린더 이벤트 제목/내용에 " - "(공백-대시-공백)가 있어서 "짧은 제목 - 부가설명" 구조이면(부가설명이 쉼표 목록이든 짧은 구절이든 상관없이 항상), 대시 앞부분만 본문 "○ " bullet으로 쓰고 대시 뒷부분은 그 아래 2칸 들여쓴 "  - " 하위 줄로 내려서 부가설명으로 적으세요. 예: 캘린더에 "산업용 PC 대여 - 웨이브다인 발송(대여증 요청)"이라고 돼 있으면
     ○ [AI확산] 산업용 PC 대여
       - 웨이브다인 발송(대여증 요청)
     캘린더에 "등급판별 센서류 탈착 - NIR(2대), 일반카메라(2대), 산업용PC 일체"라고 돼 있으면
     ○ [AI확산] 등급판별 센서류 탈착
       - NIR(2대), 일반카메라(2대), 산업용PC 일체
     로 나누세요(날짜 suffix가 있다면 위쪽 "○" 제목 줄에만 붙이고 부가설명 줄엔 붙이지 마세요).
     이벤트 설명(description)에 지난주 이전에 있었던 배경 설명(예: 일정 변경·연기 사유 등 과거 이슈)이 들어있어도, '이번 주 한 일'에는 이번 주에 실제 있었던 사실만 간결히 적고 지난주 히스토리는 옮기지 마세요(예: "[AI확산] 실무자 회의 (14:00~15:00)"처럼 담백하게).
   - '다음 주 할 일 (제안)': 이 섹션은 반드시 두 하위 소제목으로 나누세요 — 실제 사실과 AI 제안을 절대 섞지 마세요.
     ### 확정 일정
     다음 주(다음 주 월요일~금요일)에 이미 Google Calendar에 등록돼 있는 실제 이벤트만, 각 줄 앞에 "○ "를 붙여 나열하세요(제안 아님, 사실). 위 '이번 주 한 일'과 동일한 " - " 부가설명 줄바꿈 규칙을 여기도 적용하세요(제목에 " - "가 있으면 대시 뒷부분을 "  - " 하위 줄로 내림). 등록된 이벤트가 하나도 없으면 "다음 주 캘린더에 등록된 일정 없음"이라고 명시하세요.
     ### 제안 (초안)
     이 소제목 바로 아래에 반드시 '※ 아래 항목은 에이전트가 제안하는 초안이며 실제 계획이 아닙니다. 검토 후 직접 수정해주세요.'라는 문구를 넣고, 그 아래에 이번 주 기록에서 보이는 미완료·후속 작업을 "- "로 시작하는 bullet로 제안하세요. 이 소제목 아래엔 확정된 사실을 절대 넣지 말고(확정 일정은 위 소제목에만), 순수 제안·초안만 적으세요.
3. Write 도구로 이 보고서를 마크다운 파일로 저장하세요. 경로: "${REPORT_PATH}" (폴더가 없으면 Bash로 먼저 mkdir -p 하세요). 파일 맨 위에 "# ${TITLE}" 제목을 넣으세요.
4. Google Calendar MCP \`search_events\`로 calendar_id \`${CALENDAR_ID}\`에서 ${FRI} 하루 동안 제목이 "${TITLE}"인 이벤트가 이미 있는지 먼저 확인하세요(재시도 등으로 이 스크립트가 같은 주에 두 번 실행될 수 있어 중복 생성을 막기 위함입니다). 이미 있으면 그 이벤트를 \`update_event\`로 description만 아래 내용으로 갱신하고 새로 만들지 마세요. 없으면 이번 주 금요일(${FRI}) 09:00~09:30 이벤트를 하나 새로 생성하세요. 제목은 "${TITLE}", description에는 2번에서 쓴 두 섹션 내용을 그대로(요약하지 말고) 넣고 맨 끝 줄에 "전체 파일: ${REPORT_PATH}"를 추가하세요.
5. 마지막에 저장한 파일의 절대 경로와 생성한 캘린더 이벤트 ID를 각각 한 줄로 출력하세요.

참고: Google Drive MCP 도구가 연결되어 있어도 이 작업에는 쓰지 마세요 — 의도된 설계이니 항상 로컬 파일시스템(Bash/Write)으로만 저장하세요.
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
  OFFSET_BEFORE=$(wc -c < "$LOGFILE" 2>/dev/null || echo 0)
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
  ATTEMPT_OUTPUT="$(tail -c +"$((OFFSET_BEFORE + 1))" "$LOGFILE" 2>/dev/null)"

  # Calendar MCP 미인증은 `claude -p` 턴 자체는 정상 종료(exit 0)하고 캘린더
  # 작업만 조용히 실패하는 경우가 있어 EXIT_CODE 체크로는 못 잡는다 — exit
  # code와 무관하게 먼저 확인. 재시도로는 OAuth 토큰을 고칠 수 없으므로
  # fail-fast하고 재인증 방법을 안내한다.
  # 2026-07-30 확장(Codex 코드리뷰로 발견, 낮은 우선순위로 보류했다가 처리):
  # 기존 패턴이 실제 CLI가 쓸 수 있는 다른 문구(OAuth 만료/토큰 문제/MCP
  # 서버 자체 불가)는 못 잡았다 — 여전히 "grep 휴리스틱"이라 완벽하진
  # 않지만(정확한 오류 문구 카탈로그가 없어 근사할 수밖에 없음), 흔한
  # 변형들을 추가로 커버.
  if printf '%s' "$ATTEMPT_OUTPUT" | grep -qiE 'calendar.*(not authenticated|requires?.*auth|unauthorized|needs?.*(re)?auth|oauth.*(expired|invalid|fail)|token.*(missing|expired|invalid))|(not authenticated|unauthorized|oauth.*(expired|invalid)).*calendar|mcp.*(server)?.*(unavailable|not available|unreachable|connection required)'; then
    echo "attempt ${ATTEMPT}/${MAX_ATTEMPTS}: Calendar MCP 미인증 감지 — 재시도로 해결 불가, fail-fast." >> "$LOGFILE"
    bash "$HOME/mac-agent/bin/discord-notify.sh" "🔑 주간보고서 생성 실패 — Google Calendar MCP 미인증으로 보입니다. /mcp 명령으로 재인증 후 !주간보고서로 다시 요청하세요. 로그: ${LOGFILE}" || true
    exit 1
  fi

  if [ "$EXIT_CODE" -eq 0 ]; then
    SUCCESS=1
    rm -f "$DEBUG_LOGFILE"
    break
  fi

  # 계정 사용 한도 초과도 재시도로 해결 불가(한도 리셋 전까지는 몇 번을
  # 더 돌려도 동일하게 실패) — 나머지 시도를 낭비하지 않고 바로 알린다.
  # 2026-07-30 확장(Codex 코드리뷰): "hit your ... limit"/"rate_limit_error"
  # 만 잡아서 429/"rate limit exceeded"/"overloaded"/"quota exceeded" 같은
  # 표현은 놓쳤다 — 흔한 변형 추가.
  if printf '%s' "$ATTEMPT_OUTPUT" | grep -qiE 'hit your (session|usage) limit|rate.?limit(_error| exceeded)?|usage cap|quota exceeded|\boverloaded\b|\b429\b'; then
    echo "attempt ${ATTEMPT}/${MAX_ATTEMPTS}: 사용 한도 초과 감지 — 재시도로 해결 불가, fail-fast." >> "$LOGFILE"
    bash "$HOME/mac-agent/bin/discord-notify.sh" "⏳ 주간보고서 생성 실패 — 계정 사용 한도 초과. 한도 리셋 이후 !주간보고서로 다시 요청하세요. 로그: ${LOGFILE}" || true
    exit 1
  fi
done

if [ "$SUCCESS" -ne 1 ]; then
  echo "all ${MAX_ATTEMPTS} attempts failed." >> "$LOGFILE"
  # discord-notify.sh returns the posted message's Discord id on stdout (Phase 2).
  # If we get one back, record a pending-job file keyed by that id so
  # discord-bot.py can recognize a reply to THIS message and retry the script.
  MSG_ID="$(bash "$HOME/mac-agent/bin/discord-notify.sh" "⚠️ 주간보고서 생성 실패 — ${MAX_ATTEMPTS}회 재시도 모두 실패. 로그: ${LOGFILE}
이 메시지에 답장하면 다시 시도합니다." || true)"
  if [ -n "$MSG_ID" ]; then
    PENDING_DIR="$HOME/.claude/discord-bot/pending"
    mkdir -p "$PENDING_DIR"
    python3 -c "import json,sys,datetime
json.dump({'type': 'weekly-report-retry', 'created_at': datetime.datetime.now().isoformat(), 'params': {}},
           open(sys.argv[1], 'w'))" "$PENDING_DIR/${MSG_ID}.json"
  fi
  exit 1
fi
