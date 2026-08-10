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

# ANALYZE_SCRIPT가 존재하지 않으면(잘못된 배포, skill-catalog 재설치 실패 등) 아래
# 루프의 `python3 "$ANALYZE_SCRIPT" ...`가 실행될 때 인터프리터 자체의 "파일을 열 수
# 없음" exit code가 2 — 이게 analyze_sessions.py가 의도적으로 쓰는 "세션 없음(정상
# 스킵)" exit code 2와 정확히 겹친다(2026-07-29 N5 검증 중 실측 확인). 아래 루프에
# 맡겨두면 스크립트 자체가 사라진 진짜 장애가 "오늘은 세션 없네" 취급으로 조용히
# 묻혀서 디스코드 알림이 영영 안 온다 — 그래서 루프 진입 전에 파일 존재만 별도로,
# 먼저 확인한다.
if [ ! -f "$ANALYZE_SCRIPT" ]; then
  echo "--- run $(date) ---" >> "$LOGFILE"
  echo "치명적 오류: analyze_sessions.py를 찾을 수 없음 (${ANALYZE_SCRIPT})" >> "$LOGFILE"
  bash "$HOME/mac-agent/bin/telegram-notify.sh" "🚨 토큰비용 일일 리포트 전체 실패 — analyze_sessions.py 스크립트가 없음 (${ANALYZE_SCRIPT}). skill-catalog 재설치가 필요할 수 있습니다." || true
  exit 1
fi

# Repo set now lives in one place: config/tracked-repos.json (2026-07-29 —
# was a hardcoded array here that duplicated discord-bot.py's
# CODEX_REPO_ALIASES, a real drift risk since that file is edited elsewhere
# concurrently). discord-bot.py itself doesn't read this yet (deliberately
# out of scope this round — it's mid-edit elsewhere); this script is the
# first reader. Add a repo by editing the JSON (after confirming with the
# user), not by editing this script.
REPOS_JSON="$HOME/mac-agent/config/tracked-repos.json"
REPOS_LIST="$(python3 -c "
import json, os, sys
with open(sys.argv[1], encoding='utf-8') as f:
    data = json.load(f)
for name, path in data.items():
    if name.startswith('_'):
        continue
    print(f'{name}\t{os.path.expandvars(path)}')
" "$REPOS_JSON")"

echo "--- run $(date) ---" >> "$LOGFILE"
FAILED_REPOS=()
while IFS=$'\t' read -r name path; do
  [ -z "$name" ] && continue
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
done <<< "$REPOS_LIST"

if [ "${#FAILED_REPOS[@]}" -gt 0 ]; then
  FAILED_STR="$(IFS=', '; echo "${FAILED_REPOS[*]}")"
  bash "$HOME/mac-agent/bin/telegram-notify.sh" "⚠️ 토큰비용 일일 리포트 일부 실패: ${FAILED_STR}. 로그: ${LOGFILE}" || true
fi

exit 0
