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
  '{session_id:$session_id,cwd:$cwd,prompt_hash:$prompt_hash,kind:"coding",status:"gate_required",workflow_passed:false,profile_contract_version:"1.0.0",profile_contract_sha256:"ce44fbdbd9b74da1c75384322499b5648237c9fd116ace3aefe722ae8117c57a",style_version:"plain-high-school-v1",default_profile:"claude.coordinator",updated_at:$updated_at}')"

verify_task_state_write "$SESSION_ID" "$STATE_JSON" || exit 0

jq -n ' {
  hookSpecificOutput: {
    hookEventName: "UserPromptSubmit",
    additionalContext: "코드 변경 의도가 감지되었습니다. 메인 세션에서 Edit/Write를 수행하기 전에 Bash로 원문 작업을 파일에 저장하고 python3 /Users/edge_ai/mac-agent/bin/verify-task-orchestrator.py --task-file <작업파일> --cwd <cwd> --session-id <session_id> 를 실제 호출하세요. 호스트 하네스가 결정론적 점검을 수행하고 구독형 Claude·Codex·Antigravity CLI를 필요한 역할에만 호출합니다. finalVerdict가 통과한 뒤에만 직접 편집할 수 있습니다."
  }
}'
