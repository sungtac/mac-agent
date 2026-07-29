#!/usr/bin/env bash
# Runs daily 23:00 via launchd (~/Library/LaunchAgents/com.macagent.token-cost-report.plist)
# to run the token-cost-dashboard skill's analyze_sessions.py against each
# tracked repo and save a dated HTML report to Drive.
#
# Unlike weekly-report.sh / kakao-morning-briefing.sh, this does NOT need the
# watchdog+retry pattern those use for the intermittent headless `claude -p`
# hang (docs/weekly-report.md) — analyze_sessions.py is a plain python3
# script with no Claude CLI call, so that specific failure mode doesn't
# apply here. A single run with no retry loop is enough.
#
# analyze_sessions.py exit codes (fixed 2026-07-29 specifically so this
# wrapper could tell them apart): 0 = success, 2 = no session logs found for
# that repo (benign — a quiet repo that day/ever, not a bug), anything else
# (1, or an uncaught traceback) = a real failure worth a Discord ping.
set -uo pipefail

ANALYZE_SCRIPT="$HOME/.claude/skills/token-cost-dashboard/scripts/analyze_sessions.py"
DRIVE_ROOT="$HOME/Library/CloudStorage/GoogleDrive-sungtac@gmail.com/내 드라이브"
OUT_ROOT="$DRIVE_ROOT/토큰비용리포트"
STATE_DIR="$HOME/.claude/hooks-state/token-cost-report"
mkdir -p "$STATE_DIR"

TODAY="$(date +%Y-%m-%d)"
LOGFILE="$STATE_DIR/${TODAY}.log"
OUT_DIR="$OUT_ROOT/${TODAY}"
mkdir -p "$OUT_DIR"

# Same repo set as discord-bot.py's CODEX_REPO_ALIASES (!코덱스 command) —
# the set of repos already established as the ones worth tracking. Add a
# line here (after confirming with the user) to track another repo.
REPOS=(
  "mac-agent:$HOME/mac-agent"
  "hwpx-skill:$HOME/document-writing-project/hwpx-skill"
  "pptx-skill:$HOME/document-writing-project/pptx-skill"
)

echo "--- run $(date) ---" >> "$LOGFILE"
FAILED_REPOS=()
for entry in "${REPOS[@]}"; do
  name="${entry%%:*}"
  path="${entry#*:}"
  if [ ! -d "$path" ]; then
    echo "skip ${name}: 경로 없음 (${path})" >> "$LOGFILE"
    continue
  fi
  python3 "$ANALYZE_SCRIPT" "$path" --out "$OUT_DIR/${name}.html" >> "$LOGFILE" 2>&1
  code=$?
  if [ "$code" -eq 0 ]; then
    echo "${name}: 완료" >> "$LOGFILE"
  elif [ "$code" -eq 2 ]; then
    echo "${name}: 세션 없음(스킵, 정상)" >> "$LOGFILE"
  else
    echo "${name}: 실패 (exit=${code})" >> "$LOGFILE"
    FAILED_REPOS+=("$name")
  fi
done

if [ "${#FAILED_REPOS[@]}" -gt 0 ]; then
  FAILED_STR="$(IFS=', '; echo "${FAILED_REPOS[*]}")"
  bash "$HOME/mac-agent/bin/discord-notify.sh" "⚠️ 토큰비용 일일 리포트 일부 실패: ${FAILED_STR}. 로그: ${LOGFILE}" || true
fi

exit 0
