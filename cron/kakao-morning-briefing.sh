#!/usr/bin/env bash
# Runs every day 09:00 via launchd (~/Library/LaunchAgents/com.macagent.kakao-morning-briefing.plist)
# to send a KakaoTalk "나에게 보내기" morning briefing: today's Google Calendar
# events, today's weather (KMA short-term forecast, 광주광역시 grid nx=58/ny=74),
# and a news digest (종합/IT·AI/경제) via the Kakao Play MCP gateway.
#
# The Kakao side is reached through mcporter (github.com/openclaw/mcporter), a
# generic MCP client/bridge — NOT a Kakao-specific tool. Claude Code's own
# native OAuth (`claude mcp login`) was tried first and rejected the
# connection outright with "허용되지 않은 IP 대역입니다" (ERR-PLAYAUTH-90403) —
# PlayMCP's OAuth endpoint only accepts registered external-agent clients
# (mcporter is one), not Claude Code's own ad-hoc OAuth client. So the
# connection is: this script -> claude -p -> kakao-playmcp MCP server
# (registered via `claude mcp add --scope user kakao-playmcp -- mcporter serve
# --servers mcp-gateway --stdio`) -> mcporter -> PlayMCP -> KakaoTalk.
# Full story: docs/kakao-playmcp.md.
#
# mcporter's keep-alive daemon is a plain background process, NOT a launchd
# job — it does not survive a reboot on its own. `mcporter daemon start` is
# idempotent (verified 2026-07-28: prints "Daemon already running" and exits 0
# if already up), so this script starts it defensively every run rather than
# assuming it's alive — the same "don't trust prior state, just ensure it"
# posture as the PATH-absolute-path convention below.
set -uo pipefail

CLAUDE_BIN="$HOME/.local/bin/claude"
MCPORTER_BIN="/opt/homebrew/bin/mcporter"
CALENDAR_ID="sungtac@gmail.com"
STATE_DIR="$HOME/.claude/hooks-state/kakao-morning-briefing"
mkdir -p "$STATE_DIR"

# mcporter has a `#!/usr/bin/env node` shebang, and the kakao-playmcp MCP
# server registration (`claude mcp add`, see ~/.claude.json) spawns it with no
# env override — so it relies on PATH to find `node`. launchd strips PATH to
# /usr/bin:/bin:/usr/sbin:/sbin for this whole process tree (the same
# recurring gotcha as codex/agy/ffmpeg/whisper-cli/tmux/coach elsewhere in
# this repo, see docs/discord-bot.md's SUBPROCESS_ENV) — without this export,
# `mcporter daemon start` below fails with "env: node: No such file or
# directory", and the `claude -p` call further down spawns kakao-playmcp the
# same broken way, so its MCP tools never connect and every step past
# Calendar silently has nothing to call. Confirmed live 2026-07-29: exactly
# this failure in that morning's run.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

TODAY="$(date +%Y-%m-%d)"
LOGFILE="$STATE_DIR/${TODAY}.log"

# Usage pre-flight gate (2026-07-28, same rationale as weekly-report.sh):
# this script's whole job is a headless `claude -p` run, so check account
# usage before even starting the mcporter daemon. No mutex/pending-job
# machinery here (this script was never wired into discord-bot.py's
# reply-retry system) — on SKIP, just log it and send a one-way
# discord-notify.sh, same as this script's existing all-attempts-failed
# path below. Fails open on gate error (script missing, etc.) — a broken
# gate must not become a new failure point for the briefing itself.
GATE_OUTPUT="$(bash "$HOME/mac-agent/workflows/lib/usage-preflight-gate.sh" claude 2>/dev/null || echo "PROCEED (gate script error — not enforced)")"
if [[ "$GATE_OUTPUT" == SKIP:* ]]; then
  echo "usage gate: ${GATE_OUTPUT} — skipping today's briefing." >> "$LOGFILE"
  bash "$HOME/mac-agent/bin/discord-notify.sh" "⏳ 오늘 카톡 모닝 브리핑을 건너뛰었습니다 — 계정 사용량 부족.
${GATE_OUTPUT#SKIP: }" || true
  exit 4
fi

# Absolute path, not bare `mcporter` — a launchd-triggered process has PATH
# stripped to /usr/bin:/bin:/usr/sbin:/sbin (same recurring gotcha as
# codex/agy/ffmpeg/whisper-cli/tmux/coach elsewhere in this repo, see
# docs/discord-bot.md). This check is why kakao-playmcp's own `claude mcp add`
# registration also uses $MCPORTER_BIN's absolute path, not a bare command.
"$MCPORTER_BIN" daemon start >> "$LOGFILE" 2>&1

PROMPT=$(cat <<PROMPT_EOF
매일 아침 9시에 실행되는 카카오톡 모닝 브리핑 작성 작업입니다. 오늘: ${TODAY}.

절차:
1. Google Calendar MCP 도구로 calendar_id \`${CALENDAR_ID}\`에서 오늘(${TODAY}) 하루의 일정을 가져오세요. '대한민국의 휴일' 같은 공휴일 캘린더 이벤트는 제외하세요. 일정이 없으면 "오늘 등록된 일정 없음"으로 처리하세요.
2. kakao-playmcp MCP 서버의 20-get_short_term_forcast 도구로 오늘 날씨를 조회하세요. base_date는 ${TODAY//-/}(YYYYMMDD 형식), nx는 58, ny는 74로 고정해서 호출하세요(광주광역시 기상청 격자좌표, 이미 검증된 값 — 절대 다른 값으로 바꾸지 마세요). 응답에서 오늘 낮 기온(TMP)과 하늘상태(SKY: 1=맑음/3=구름많음/4=흐림), 강수확률(POP)을 뽑아 한 줄로 요약하세요.
3. kakao-playmcp MCP 서버의 KakaoPNB-summarize_news 도구를 topic 값을 바꿔가며 3번 호출하세요: "종합", "IT/AI", "경제". 이 도구는 최종 요약이 아니라 "요약을 작성하라"는 지시문 + 원본 기사 목록을 반환합니다 — 그 지시문을 그대로 따라 각 주제별로 네가 직접 요약을 작성하세요. 단, 원 도구 지시문의 500~800자 분량 기준은 무시하고, 카카오톡 메시지 전체 가독성을 위해 각 주제당 2~3문장(핵심 헤드라인 위주)으로 압축하세요.
4. 위 1~3의 내용을 하나의 카카오톡 메시지로 합쳐서 작성하세요. 형식:
   [오늘의 브리핑 ${TODAY}]
   📅 일정
   (1번 내용)
   ☀️ 날씨
   (2번 내용)
   📰 뉴스
   - 종합: (요약)
   - IT/AI: (요약)
   - 경제: (요약)
5. kakao-playmcp MCP 서버의 KakaotalkChat-MemoChat 도구로 4번에서 만든 메시지를 나에게 보내세요. (메시지 길이 제한은 문서상 200자로 적혀 있지만 실측 결과 더 긴 텍스트도 정상 전송됨 — 확인됨, 인위적으로 자르지 마세요.)
6. 마지막으로 "카카오톡 발송 완료"라고만 한 줄 출력하세요. 실패한 단계가 있으면 어떤 단계에서 무엇이 실패했는지 명시하세요.
PROMPT_EOF
)

# Watchdog + retry: mirrors cron/weekly-report.sh's mitigation for the
# intermittent launchd-triggered `claude -p` hang (confirmed there via live
# repro — a stall inside the Bun-based CLI's own HTTP connection pool, not a
# network/DNS/proxy issue). Same shape here since this script shares the exact
# same headless-claude-under-launchd execution path.
#
# CONFIRM_MARKER check (added after a code-level review, 2026-07-28): claude
# -p's own exit code only reflects whether the CLI process itself completed
# without crashing — NOT whether the KakaoTalk send actually succeeded. If the
# kakao-playmcp MCP tool call fails inside (e.g. the mcporter daemon died),
# Claude can still finish its turn cleanly (exit 0) while its own text says it
# failed (per the prompt's step 6 instruction to report failures instead of
# the confirmation line). Treating exit=0 alone as success would silently
# skip both the retry loop AND the Discord escalation on a real delivery
# failure — the user just wouldn't get their briefing with zero signal
# anything went wrong. So success now additionally requires the literal
# confirmation string from the prompt's step 6 to be present in that
# attempt's own output (captured per-attempt, not grepped from the
# cross-attempt LOGFILE, so a later attempt can't accidentally match on an
# earlier attempt's success/failure text).
#
# Exact-match, not substring (fixed 2026-07-29 after a live false positive):
# a plain `grep -qF` for the marker matched even when the model's own FAILURE
# explanation happened to quote the phrase, e.g. "**카카오톡 발송 완료** —
# 하지 못했습니다." — the substring is present even though the sentence
# negates it. That false positive is what let a genuinely failed attempt 2
# end the retry loop early (skipping attempt 3 and the all-attempts-failed
# Discord notification) with the KakaoTalk message never actually sent. The
# prompt's step 6 already asks for the marker ALONE as the final line on
# success ("...라고만 한 줄 출력"), so requiring the last non-blank line to
# match exactly closes this without weakening the success bar.
CONFIRM_MARKER="카카오톡 발송 완료"
TIMEOUT_SECONDS=240
MAX_ATTEMPTS=3
SUCCESS=0
for ATTEMPT in $(seq 1 "$MAX_ATTEMPTS"); do
  DEBUG_LOGFILE="$STATE_DIR/${TODAY}-attempt${ATTEMPT}-debug.log"
  ATTEMPT_OUTFILE="$STATE_DIR/${TODAY}-attempt${ATTEMPT}.out"
  echo "--- attempt ${ATTEMPT}/${MAX_ATTEMPTS} ---" >> "$LOGFILE"
  WORK_LOG_DISPATCHED=1 "$CLAUDE_BIN" -p "$PROMPT" --output-format text \
    --debug-file "$DEBUG_LOGFILE" </dev/null > "$ATTEMPT_OUTFILE" 2>&1 &
  CLAUDE_PID=$!
  ELAPSED=0
  TIMED_OUT=0
  while kill -0 "$CLAUDE_PID" 2>/dev/null; do
    if [ "$ELAPSED" -ge "$TIMEOUT_SECONDS" ]; then
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
  cat "$ATTEMPT_OUTFILE" >> "$LOGFILE"
  if [ "$TIMED_OUT" -eq 1 ]; then
    echo "attempt ${ATTEMPT}/${MAX_ATTEMPTS}: TIMEOUT after ${TIMEOUT_SECONDS}s — killed. Debug log: ${DEBUG_LOGFILE}." >> "$LOGFILE"
    # Idempotency guard (2026-07-29): unlike weekly-report.sh's Calendar
    # event (dedup'd via search_events-before-create) or work-log-stop-check.sh's
    # archive copy (idempotent, same source -> same dest overwrite), a
    # KakaoTalk "나에게 보내기" send is a one-way fire with no way to query
    # "was this already sent". Blindly retrying on ANY timeout risks a
    # duplicate: if the send tool call (step 5, KakaotalkChat-MemoChat) had
    # already succeeded before the watchdog killed this attempt (e.g. it
    # hung afterward during step 6's own text generation), attempt 2 would
    # resend the whole briefing from scratch. Grep the just-killed attempt's
    # own debug log for evidence the send tool was actually invoked before
    # the kill — if so, the outcome is ambiguous (may have sent, may have
    # failed inside kakao-playmcp) and resending guarantees a duplicate if it
    # did succeed, so don't retry; escalate instead. Same "under-logging
    # beats spamming" posture already used by work-log-stop-check.sh for its
    # own ambiguous-classification case.
    if [ -f "$DEBUG_LOGFILE" ] && grep -q 'KakaotalkChat-MemoChat' "$DEBUG_LOGFILE" 2>/dev/null; then
      echo "attempt ${ATTEMPT}/${MAX_ATTEMPTS}: 타임아웃 직전 카톡 발송 도구 호출 흔적이 debug 로그에 있음 — 이미 보냈을 수 있어 재시도(중복발송 위험) 대신 여기서 중단." >> "$LOGFILE"
      bash "$HOME/mac-agent/bin/discord-notify.sh" "⚠️ 카톡 모닝 브리핑 — ${ATTEMPT}번째 시도가 타임아웃됐는데, 발송 직전까지 진행된 흔적이 있어 재시도하지 않았습니다(중복발송 방지). 실제로 도착했는지 확인해주세요. 로그: ${LOGFILE}" || true
      exit 3
    fi
    rm -f "$ATTEMPT_OUTFILE"
    sleep 5
    continue
  fi
  wait "$CLAUDE_PID"
  EXIT_CODE=$?
  echo "attempt ${ATTEMPT}/${MAX_ATTEMPTS}: exit=${EXIT_CODE}" >> "$LOGFILE"
  LAST_LINE="$(grep -v '^[[:space:]]*$' "$ATTEMPT_OUTFILE" | tail -1 | tr -d '\r')"
  if [ "$EXIT_CODE" -eq 0 ] && [ "$LAST_LINE" = "$CONFIRM_MARKER" ]; then
    SUCCESS=1
    rm -f "$DEBUG_LOGFILE" "$ATTEMPT_OUTFILE"
    break
  fi
  if [ "$EXIT_CODE" -eq 0 ]; then
    echo "attempt ${ATTEMPT}/${MAX_ATTEMPTS}: exit=0 but last line \"${LAST_LINE}\" does not exactly match confirmation string \"${CONFIRM_MARKER}\" — treating as failed attempt." >> "$LOGFILE"
  fi
  rm -f "$ATTEMPT_OUTFILE"
done

if [ "$SUCCESS" -ne 1 ]; then
  echo "all ${MAX_ATTEMPTS} attempts failed." >> "$LOGFILE"
  bash "$HOME/mac-agent/bin/discord-notify.sh" "⚠️ 카톡 모닝 브리핑 실패 — ${MAX_ATTEMPTS}회 재시도 모두 실패. 로그: ${LOGFILE}" || true
  exit 1
fi
