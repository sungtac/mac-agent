#!/usr/bin/env bash
# Stop hook: nags (once per session) if this session did substantive work
# without any verify-task invocation appearing in the transcript.
#
# This is a MECHANICAL PROXY, not real judgment. A shell script can't tell a
# trivial edit from an important one — it only counts tool calls and greps
# Bash commands against a keyword list.
#
# Two tiers:
#   - MANDATORY category registry (2026-07-27, user-confirmed): if a session
#     matches one of these, verify-task MUST have run — no "explain why you
#     skipped it" escape. Grown one item at a time as the user identifies
#     more (see docs/verify-task-stop-check.md):
#       1. 코딩 (coding)     — 3+ Edit/Write tool calls this session.
#       2. 아이디어 회의 (idea/design meeting) — ExitPlanMode invoked this
#          session (best mechanical proxy available right now — a design
#          discussion that never enters Plan Mode isn't caught by this yet;
#          known gap, not a solved detector).
#   - SOFT category (pre-existing, escape valve intact): a risky Bash
#     command (git commit/push, brew/npm/pip install, plugin install,
#     marketplace add, rm -rf, chmod +x) with no mandatory-category hit —
#     agent may explain to the user why verification was skipped instead.
#
# It nags at most once per session_id (a marker file), so it can't loop
# forever if the answer is genuinely "this didn't need it" (soft tier only).
set -uo pipefail

INPUT="$(cat)"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)"
TRANSCRIPT_PATH="$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)"

[ -z "$SESSION_ID" ] && exit 0

STATE_DIR="$HOME/.claude/hooks-state/verify-task-nag"
mkdir -p "$STATE_DIR"
NAG_MARKER="$STATE_DIR/${SESSION_ID}.nagged"

# housekeeping: prune markers older than a week
find "$STATE_DIR" -type f -mtime +7 -delete 2>/dev/null || true

# already nagged this session once -> let it stop
[ -f "$NAG_MARKER" ] && exit 0

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  TRANSCRIPT_PATH="$(find "$HOME/.claude/projects" -name "${SESSION_ID}.jsonl" 2>/dev/null | head -1)"
fi
if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  exit 0
fi

# Parsed with jq (already a hard dependency here), same reasoning as
# HAS_VERIFY_TASK below: raw-JSON key grepping breaks silently if the
# transcript serializer's whitespace/key-ordering ever shifts — that's
# exactly the failure mode that defeated HAS_VERIFY_TASK before its own fix.
EDIT_WRITE_COUNT="$(jq -r '
  select(.type=="assistant") | .message.content[]? | select(.type=="tool_use") |
  select(.name=="Edit" or .name=="Write") | .name
' "$TRANSCRIPT_PATH" 2>/dev/null | wc -l | tr -d ' ')"
RISKY_BASH_COUNT="$(jq -r '
  select(.type=="assistant") | .message.content[]? | select(.type=="tool_use") |
  select(.name=="Bash") | (.input.command // "")
' "$TRANSCRIPT_PATH" 2>/dev/null | grep -cE 'git commit|git push|brew install|npm install|pip install|pip3 install|plugin install|marketplace add|rm -rf|chmod \+x')"
HAS_PLAN_MODE="$(jq -r '
  select(.type=="assistant") | .message.content[]? | select(.type=="tool_use") |
  select(.name=="ExitPlanMode") | .name
' "$TRANSCRIPT_PATH" 2>/dev/null | wc -l | tr -d ' ')"
# Fixed 2026-07-29: the old pattern (bare `grep -c 'verify-task'`) matches
# the literal word "verify-task" anywhere in the transcript — including
# boilerplate that has nothing to do with actually running it (every
# session's system-reminder skill_listing describes verify-task/
# verify-task-v2 in its one-line skill catalog, and any doc path or prose
# mentioning the name matches too). A real-world audit of every session log
# for this project found this pattern matching constantly on boilerplate
# while the real host orchestrator invocation was absent. That means this
# MANDATORY tier's "no escape valve" guarantee was silently defeated almost
# every session: the exemption fired on the boilerplate mention alone, so
# MANDATORY_HITS below was never reached. Parsed with jq (already a hard
# dependency here) so it walks the real message.content[].{type,name,input}
# structure instead of guessing at raw-JSON key ordering/whitespace.
# 2026-07-30 fix (Codex 코드리뷰로 발견): `grep -c 'verify-task'`는 여전히
# 부분 문자열 매칭이라 존재하지도 않는 가짜 경로나 무관한 스크립트도
# 통과시켰다. 이제는 정식 호스트 오케스트레이터의 절대 경로만 인정한다.
# 여전히 "성공했는지"는 안 보는 기계적 프록시다(파일 헤더 주석 참고) —
# 그 이상의 결과 검증은 이 훅의 설계 범위 밖.
#
HAS_VERIFY_TASK="$(jq -r '
  select(.type=="assistant") | .message.content[]? | select(.type=="tool_use") |
  select(.name == "Bash") | (.input.command // "")
' "$TRANSCRIPT_PATH" 2>/dev/null | grep -cE '(^|[[:space:]])python3[[:space:]]+/Users/edge_ai/mac-agent/bin/verify-task-orchestrator\.py([[:space:]]|$)')"

[ "${HAS_VERIFY_TASK:-0}" -gt 0 ] && exit 0

MANDATORY_HITS=()
[ "${EDIT_WRITE_COUNT:-0}" -ge 3 ] && MANDATORY_HITS+=("코딩(Edit/Write ${EDIT_WRITE_COUNT}회)")
[ "${HAS_PLAN_MODE:-0}" -gt 0 ] && MANDATORY_HITS+=("아이디어 회의(ExitPlanMode 사용)")

if [ "${#MANDATORY_HITS[@]}" -gt 0 ]; then
  touch "$NAG_MARKER"
  HITS_STR="$(IFS=', '; echo "${MANDATORY_HITS[*]}")"
  jq -n --arg hits "$HITS_STR" '{
    decision: "block",
    reason: ("이 세션은 무조건 독립검증 대상 카테고리(" + $hits + ")에 해당하는데 호스트 검증 오케스트레이터 실행 흔적이 없습니다. 이 카테고리는 \"검증 필요 없었다\"는 설명으로 넘어갈 수 없습니다 — python3 /Users/edge_ai/mac-agent/bin/verify-task-orchestrator.py --task-file <작업파일> --cwd <cwd> --session-id <session_id> 를 실제로 실행하세요(작은 변경이면 light 트랙이 자동으로 저렴하게 처리합니다)."),
    systemMessage: "무조건 독립검증 카테고리 감지 — verify-task 실행 필수, 생략 불가."
  }'
  exit 0
fi

if [ "${RISKY_BASH_COUNT:-0}" -ge 1 ]; then
  touch "$NAG_MARKER"
  jq -n '{
    decision: "block",
    reason: "이 세션에서 설치/커밋 등 위험 신호가 있는 Bash 명령이 감지됐는데, verify-task로 독립 검증한 흔적이 안 보입니다. 이번 작업이 검증 대상(중요/복잡한 작업)이라면 python3 /Users/edge_ai/mac-agent/bin/verify-task-orchestrator.py --task-file <작업파일> --cwd <cwd> --session-id <session_id> 로 호스트 검증을 돌리세요(작은 변경이면 light 트랙이 자동으로 저렴하게 처리합니다). 검증이 필요 없는 가벼운 작업이었다면, 그 이유를 사용자에게 한 줄로 알려주고 종료하세요.",
    systemMessage: "verify-task 미실행 감지 — 검증하거나, 왜 생략하는지 사용자에게 알려주세요."
  }'
fi

exit 0
