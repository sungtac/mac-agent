#!/usr/bin/env bash
# UserPromptSubmit hook: mark coding prompts as requiring verify-task-v2.
# This hook is intentionally fast.  It never invokes a provider or Workflow.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hooks/lib/verify-task-state.sh
source "$SCRIPT_DIR/lib/verify-task-state.sh"

INPUT="$(cat)"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)"
CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)"
PROMPT="$(printf '%s' "$INPUT" | jq -r '.prompt // empty' 2>/dev/null)"

[ -n "$SESSION_ID" ] && [ -n "$CWD" ] && [ -n "$PROMPT" ] || exit 0

# The classifier is deliberately conservative about ordinary questions, but
# broad enough to catch Korean and English implementation/review requests.
if ! printf '%s' "$PROMPT" | grep -Eiq '구현|수정|변경|코딩|코드 리뷰|리팩|버그|테스트 추가|파일에 작성|implement|modify|change|refactor|bug|code review|write code|patch|debug'; then
  exit 0
fi

mkdir -p "$VERIFY_TASK_STATE_ROOT" 2>/dev/null || exit 0
find "$VERIFY_TASK_STATE_ROOT" -maxdepth 1 -type f -name '*.json' -mtime +7 -delete 2>/dev/null || true

PROMPT_HASH="$(verify_task_hash "$PROMPT")"
STATE_JSON="$(jq -n \
  --arg session_id "$SESSION_ID" \
  --arg cwd "$CWD" \
  --arg prompt_hash "$PROMPT_HASH" \
  --arg updated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{session_id:$session_id,cwd:$cwd,prompt_hash:$prompt_hash,kind:"coding",status:"gate_required",workflow_passed:false,updated_at:$updated_at}')"

verify_task_state_write "$SESSION_ID" "$STATE_JSON" || exit 0

jq -n ' {
  hookSpecificOutput: {
    hookEventName: "UserPromptSubmit",
    additionalContext: "코드 변경 의도가 감지되었습니다. 메인 세션에서 Edit/Write를 수행하기 전에 반드시 Workflow({scriptPath:\"~/.claude/workflows/verify-task-v2.js\", args:{task, cwd, persona, sessionId}})를 실제 호출하세요. Workflow의 조사·병렬 계획 검토·Codex 구현·code-review가 성공한 뒤에만 직접 편집할 수 있습니다."
  }
}'
