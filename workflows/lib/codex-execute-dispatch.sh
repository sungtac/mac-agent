#!/usr/bin/env bash
# Deterministic dispatcher for a WRITE-CAPABLE Codex execution step (verify-task
# v2's "코덱스 실행" — Full track Step 3). Distinct from score-dispatch.sh,
# which only ever runs Codex/Antigravity read-only for scoring/opinions.
#
# Why this is a separate script instead of reusing score-dispatch.sh: that
# script always invokes `codex exec` with the default read-only sandbox,
# because every caller of it wants a scoring/critique opinion, never a real
# file write. This dispatcher runs Codex with `-s workspace-write -C <cwd>`
# so it can actually implement the synthesized instruction as real file
# changes in the target repo — a materially different, higher-blast-radius
# operation that deserves its own explicit call site rather than a silent
# mode flag on the scoring dispatcher.
#
# Same safety shape as score-dispatch.sh: prompt content is never
# interpolated into a shell command string (passed via a file, read with
# `cat`, handed to `codex exec` as a single argv element) — no shell
# injection vector from task/spec/feedback text.
#
# This does NOT try to extract a structured verdict from Codex's output —
# unlike scoring, "what Codex actually changed" is never trusted from its own
# self-report. The caller must independently verify via a real `git diff`
# after this returns (see gatherVerificationContext() in verify-task-v2.js).
# This script only reports whether the process itself completed cleanly.
set -uo pipefail

CWD="${1:?usage: codex-execute-dispatch.sh <cwd> <prompt-file>}"
PROMPT_FILE="${2:?usage: codex-execute-dispatch.sh <cwd> <prompt-file>}"

FAILURE_ENVELOPE() {
  local reason="$1"
  python3 - "$reason" << 'PYEOF'
import json, sys
reason = sys.argv[1]
print(json.dumps({"ok": False, "message": reason}, ensure_ascii=False))
PYEOF
}

if [ ! -d "$CWD" ]; then
  FAILURE_ENVELOPE "작업 디렉토리를 찾을 수 없음: $CWD"
  exit 0
fi

if [ ! -f "$PROMPT_FILE" ]; then
  FAILURE_ENVELOPE "프롬프트 파일을 찾을 수 없음: $PROMPT_FILE"
  exit 0
fi

# CWD must be a real git repo (2026-07-29, found in code review before ever
# hit live): every caller of this script (verify-task-v2.js's own design
# doc says cwd is mandatory specifically so it can run `git status`/`git
# diff` unconditionally; discord-bot.py's !코덱스 verifies via before/after
# `git diff`/`git status` snapshots, never trusting Codex's self-report)
# already assumes this. Without this check, a non-git $CWD would silently
# defeat that entire verification model: `git diff`/`git status` against a
# non-repo print usage/fatal-error text (confirmed via local repro) that
# the caller's line-based parser doesn't recognize, so it just sees an
# empty diff and reports "no changes" even if Codex actually wrote files —
# a completely silent safety-net failure, not an error. Explicit check here
# closes that instead of relying on it never happening to be hit (today's
# CODEX_REPO_ALIASES only points at real repos, but nothing enforced that).
if ! /usr/bin/git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  FAILURE_ENVELOPE "작업 디렉토리가 git 저장소가 아님: $CWD (이 스크립트를 호출하는 쪽의 diff 기반 검증이 git 저장소를 전제하므로, 아닐 경우 실행하지 않음)"
  exit 0
fi

# Absolute path, not bare `codex` — a Workflow-spawned agent's Bash
# environment can have a PATH stripped of /opt/homebrew/bin (see score-dispatch.sh).
# No --skip-git-repo-check — the check above already guarantees this is a
# real git repo, so there's nothing to skip; keeping the flag would have
# silently masked the check above's own guarantee if the two ever drifted.
RAW_OUTPUT="$(/opt/homebrew/bin/codex exec -s workspace-write -C "$CWD" "$(cat "$PROMPT_FILE")" 2>&1)"
EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 0 ]; then
  # tail, not head (2026-07-29, found in review before ever hit live): a
  # verbose codex run (banner + per-tool-call chatter) puts its actual
  # failure reason near the END of RAW_OUTPUT, not the start — confirmed by
  # simulating a realistic-length failing run, where the real error text
  # fell entirely outside a `head -c 2000` cut but was still present in the
  # last 2000 chars. The success path below already uses `raw[-4000:]`
  # (tail) for the same reason; this was the one path still cutting from
  # the wrong end, meaning most real failures likely showed generic
  # boilerplate instead of the actual error.
  TRUNCATED="$(printf '%s' "$RAW_OUTPUT" | tail -c 2000)"
  FAILURE_ENVELOPE "codex exec 종료코드 ${EXIT_CODE}. 원본 출력(끝 2000자): ${TRUNCATED}"
  exit 0
fi

python3 - "$RAW_OUTPUT" << 'PYEOF'
import json, sys
raw = sys.argv[1]
print(json.dumps({"ok": True, "message": raw[-4000:]}, ensure_ascii=False))
PYEOF
