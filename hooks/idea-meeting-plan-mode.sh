#!/usr/bin/env bash
# UserPromptSubmit hook: if the user's prompt contains the literal keyword
# "아이디어 회의" (idea/design meeting), inject a strong instruction to enter
# Plan Mode immediately.
#
# Why: hooks/verify-task-stop-check.sh's MANDATORY "아이디어 회의" category
# (2026-07-27) detects whether a design discussion happened via ExitPlanMode
# appearing in the transcript — but that detector only has something to catch
# if Plan Mode actually gets entered. A discussion that never calls
# EnterPlanMode slips through undetected. This hook is the other half: tie
# the same keyword to actually entering Plan Mode.
#
# A hook can't force a tool call — this only injects additionalContext
# nudging the model to call EnterPlanMode. If ignored, the Stop-hook
# backstop (verify-task-stop-check.sh) still blocks unconditionally at
# session end, no escape valve, same as the coding category.
set -uo pipefail

INPUT="$(cat)"
PROMPT="$(printf '%s' "$INPUT" | jq -r '.prompt // empty' 2>/dev/null)"

[ -z "$PROMPT" ] && exit 0

case "$PROMPT" in
  *"아이디어 회의"*)
    jq -n '{
      hookSpecificOutput: {
        hookEventName: "UserPromptSubmit",
        additionalContext: "사용자가 \"아이디어 회의\" 키워드를 썼습니다 — 다른 작업보다 먼저 EnterPlanMode를 호출해 계획 모드로 들어가세요. 이 세션은 verify-task-stop-check.sh의 무조건 독립검증 카테고리(아이디어 회의) 대상이며, ExitPlanMode 호출 여부로 검증 대상 인식 여부를 판단합니다."
      }
    }'
    ;;
esac

exit 0
