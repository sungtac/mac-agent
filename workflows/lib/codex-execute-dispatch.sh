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

# Absolute path, not bare `codex` — a Workflow-spawned agent's Bash
# environment can have a PATH stripped of /opt/homebrew/bin (see score-dispatch.sh).
RAW_OUTPUT="$(/opt/homebrew/bin/codex exec --skip-git-repo-check -s workspace-write -C "$CWD" "$(cat "$PROMPT_FILE")" 2>&1)"
EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 0 ]; then
  TRUNCATED="$(printf '%s' "$RAW_OUTPUT" | head -c 2000)"
  FAILURE_ENVELOPE "codex exec 종료코드 ${EXIT_CODE}. 원본 출력(앞 2000자): ${TRUNCATED}"
  exit 0
fi

python3 - "$RAW_OUTPUT" << 'PYEOF'
import json, sys
raw = sys.argv[1]
print(json.dumps({"ok": True, "message": raw[-4000:]}, ensure_ascii=False))
PYEOF
