#!/usr/bin/env bash
# Deterministic dispatcher for invoking an external scoring CLI (codex or agy)
# with a prompt file, and normalizing the result to a JSON envelope.
#
# Why this exists: asking an LLM sub-agent to "run this exact shell command"
# built from natural-language instructions is fragile — it's prone to shell
# injection (untrusted text interpolated into a command string) and to
# fabricated results (an LLM told "if parsing fails, invent a score" will
# invent a score). This script removes both failure modes structurally:
# arguments are passed as real argv (no string interpolation of prompt
# content into shell syntax), and a parse/exec failure always produces the
# exact same fixed JSON envelope, built in code — never a plausible-looking
# LLM guess.
set -uo pipefail

TOOL="${1:?usage: score-dispatch.sh <codex|agy> <prompt-file>}"
PROMPT_FILE="${2:?usage: score-dispatch.sh <codex|agy> <prompt-file>}"

FAILURE_ENVELOPE() {
  local reason="$1"
  python3 - "$reason" << 'PYEOF'
import json, sys
reason = sys.argv[1]
print(json.dumps({
    "scores": {"목표달성도": 0, "정확성": 0, "제약안전성": 0, "완성도": 0, "명확성": 0, "효율성": 0},
    "total": 0,
    "dealbreaker": True,
    "dealbreaker_reason": "채점 도구 실행/파싱 실패 — 작업 내용에 대한 판단 아님",
    "feedback": reason,
}, ensure_ascii=False))
PYEOF
}

if [ ! -f "$PROMPT_FILE" ]; then
  FAILURE_ENVELOPE "프롬프트 파일을 찾을 수 없음: $PROMPT_FILE"
  exit 0
fi

# Both codex and agy are invoked by absolute path, not bare command name —
# confirmed 2026-07-26 that a Workflow-spawned agent's Bash environment can
# have a stripped PATH missing /opt/homebrew/bin (same recurring gotcha as
# tmux/coach/claude/ffmpeg/whisper-cli elsewhere on this Mac), which silently
# turns "codex not found" into a fabricated preflight/scoring failure instead
# of an actual tool problem.
case "$TOOL" in
  codex)
    RAW_OUTPUT="$(/opt/homebrew/bin/codex exec --skip-git-repo-check "$(cat "$PROMPT_FILE")" 2>&1)"
    ;;
  agy)
    RAW_OUTPUT="$(env -u SSH_CONNECTION -u SSH_TTY -u SSH_CLIENT /Users/edge_ai/.local/bin/agy -p "$(cat "$PROMPT_FILE")" 2>&1)"
    ;;
  *)
    FAILURE_ENVELOPE "알 수 없는 도구: $TOOL (codex 또는 agy만 지원)"
    exit 0
    ;;
esac

EXTRACTED="$(printf '%s' "$RAW_OUTPUT" | python3 -c '
import sys, json

def find_last_valid_json(text):
    # Tool CLIs (codex/agy) print banner/reasoning noise around the answer,
    # sometimes wrapped in markdown code fences, sometimes pretty-printed
    # across multiple lines. Brace-match every "{" to find balanced
    # candidates, validate each with json.loads, and keep the last (i.e.
    # rightmost / final) one that actually parses. This is robust to both
    # single-line and pretty-printed JSON, and ignores fence characters
    # since they are just non-brace text to the matcher.
    candidates = []
    n = len(text)
    for start in range(n):
        if text[start] != "{":
            continue
        depth = 0
        for end in range(start, n):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:end + 1]
                    try:
                        json.loads(candidate)
                        candidates.append(candidate)
                    except Exception:
                        pass
                    break
    return candidates[-1] if candidates else None

result = find_last_valid_json(sys.stdin.read())
if result:
    print(result)
else:
    sys.exit(1)
' 2>/dev/null)"

if [ -n "$EXTRACTED" ]; then
  printf '%s\n' "$EXTRACTED"
else
  TRUNCATED="$(printf '%s' "$RAW_OUTPUT" | head -c 2000)"
  FAILURE_ENVELOPE "도구 출력에서 유효한 JSON을 찾지 못함. 원본 출력(앞 2000자): ${TRUNCATED}"
fi
