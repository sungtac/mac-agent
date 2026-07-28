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

TOOL="${1:?usage: score-dispatch.sh <codex|agy> <prompt-file> [schema-kind]}"
PROMPT_FILE="${2:?usage: score-dispatch.sh <codex|agy> <prompt-file> [schema-kind]}"
# schema-kind: which caller's JSON schema the failure envelope must satisfy
# (its required fields). Optional, defaults to "rubric" — verify-task.js (v1)
# calls this script with only 2 args, so it silently keeps getting the
# original rubric-shaped envelope unchanged. verify-task-v2.js (v1's sibling,
# many different schemas per stage) passes this explicitly. Discovered
# 2026-07-27/28: a single fixed rubric-shaped envelope only matches v1's
# schema — every v2 stage schema (plan/critique/reconcile/review/light-eval)
# is structurally different, so a v1-shaped failure envelope would itself
# fail v2's schema validation instead of surfacing as a clean, retryable
# "dispatch failed" signal. docs/verify-task-v2-design.md "손 안 댄 것" 기록.
SCHEMA_KIND="${3:-rubric}"

FAILURE_ENVELOPE() {
  local reason="$1"
  python3 - "$reason" "$SCHEMA_KIND" << 'PYEOF'
import json, sys
reason = sys.argv[1]
kind = sys.argv[2]

# v1(verify-task.js) 계약: 정확히 이 다섯 필드, dealbreaker_reason 문구까지
# 그대로 — v1의 isDispatchFailure()가 이 문구를 하드코딩해서 비교하므로
# 절대 바꾸면 안 됨(둘이 반드시 동기화돼야 하는 상수, verify-task.js에 동일한
# 경고 주석 있음). 이 kind는 새 필드를 추가하지 않는다 — 방금 실측 검증한
# v1 경로에 아무 부작용도 안 남기기 위해 기존 그대로 둠.
if kind == "rubric":
    envelope = {
        "scores": {"목표달성도": 0, "정확성": 0, "제약안전성": 0, "완성도": 0, "명확성": 0, "효율성": 0},
        "total": 0,
        "dealbreaker": True,
        "dealbreaker_reason": "채점 도구 실행/파싱 실패 — 작업 내용에 대한 판단 아님",
        "feedback": reason,
    }
    print(json.dumps(envelope, ensure_ascii=False))
    sys.exit(0)

# v2(verify-task-v2.js) 각 단계 스키마의 required 필드를 정확히 채워서
# 그 단계의 JSON Schema 검증을 통과시키고, 공통 "dispatchFailed" 마커로
# 도구 실패를 스키마와 무관하게 판별 가능하게 한다(문자열 문구 동기화가
# 아니라 boolean 필드 하나면 되므로 v1보다 더 견고함).
V2_ENVELOPES = {
    "light-eval": {
        "completionCriteria": "",
        "total": 0,
        "escapeHatch": False,
        "escapeHatchReason": "",
        "feedback": reason,
    },
    "plan": {
        "needsClarification": False,
        "clarifyingQuestions": "",
        "plan": "",
    },
    # issues를 빈 배열로 두면 "안티그래비티가 검토해서 문제 없다고 함"으로
    # 오독될 수 있음(fail-open) — 대신 실패 자체를 이슈 하나로 넣어서
    # 취합(reconcile) 단계에서 "이 비평은 실행 자체가 안 됐다"는 사실이
    # 눈에 띄게 남도록 한다(fail-closed).
    "critique": {
        "issues": [{"description": f"[도구 실패] 비평 자체가 실행되지 않음 — {reason}", "severity": "tooling-failure"}],
        "notes": reason,
    },
    "reconcile": {
        "compiledIssues": [],
        "disagreements": reason,
        "revisedPlan": "",
    },
    # hasBlockingIssue=True는 의도적 fail-closed 기본값 — 재시도 로직이
    # dispatchFailed 마커를 못 잡고 이 값이 그대로 새어나가더라도, "이상
    # 없음(false)"으로 조용히 통과시키는 것보다 "블로킹 이슈 있음"으로
    # 잡혀서 사용자 눈에 띄는 쪽이 훨씬 안전하다.
    "review": {
        "hasBlockingIssue": True,
        "issues": [{"description": reason, "blocking": True}],
        "notes": reason,
    },
}
envelope = V2_ENVELOPES.get(kind, V2_ENVELOPES["review"])
envelope["dispatchFailed"] = True
envelope["dispatchFailureReason"] = reason
print(json.dumps(envelope, ensure_ascii=False))
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
