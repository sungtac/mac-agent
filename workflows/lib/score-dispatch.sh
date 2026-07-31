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
# schema — every v2 stage schema (plan/critique/reconcile/review/light-eval/
# nano-plan)
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
    "nano-plan": {
        "steps": [],
    },
    "research": {
        "focus": "",
        "findings": f"[도구 실패] 조사 자체가 실행되지 않음 — {reason}",
        "evidence": [],
        "risks": [reason],
        "testImplications": [],
    },
    "plan-review": {
        "reviewerFocus": "",
        "issues": [{"description": f"[도구 실패] 계획 검토 자체가 실행되지 않음 — {reason}", "severity": "tooling-failure"}],
        "approvedParts": [],
        "notes": reason,
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
        "checks": [{"name": "review-dispatch", "status": "error", "evidence": reason}],
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

# Both codex and agy are invoked by a resolved absolute path, not a bare
# command name —
# confirmed 2026-07-26 that a Workflow-spawned agent's Bash environment can
# have a stripped PATH missing /opt/homebrew/bin (same recurring gotcha as
# tmux/coach/claude/ffmpeg/whisper-cli elsewhere on this Mac), which silently
# turns "codex not found" into a fabricated preflight/scoring failure instead
# of an actual tool problem. The absolute paths below are this machine's
# known-good defaults, but they're overridable via CODEX_BIN/AGY_BIN env vars
# (not hardcoded-only) so the same script still works if ported to another
# agent/machine with different install locations — set-and-forget default,
# not a portability dead end.
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=provider-bin.sh
. "$SCRIPT_DIR/provider-bin.sh"
PROVIDER_SANDBOX="$SCRIPT_DIR/../../bin/edge-agent-provider-sandbox.sh"
CODEX_BIN="${CODEX_BIN:-}"
AGY_BIN="${AGY_BIN:-}"
[ -n "$CODEX_BIN" ] || CODEX_BIN="$(find_codex_bin || true)"
[ -n "$AGY_BIN" ] || AGY_BIN="$(find_agy_bin || true)"

PROMPT_CONTENT="$(cat "$PROMPT_FILE")"
CAPABILITY_RESOLVER="$SCRIPT_DIR/../../bin/edge_agent_capability_registry.py"
if [ -r "$CAPABILITY_RESOLVER" ]; then
  PROMPT_CONTENT="$(python3 "$CAPABILITY_RESOLVER" --prompt "$(cat "$PROMPT_FILE")"; printf '\n\n[검증 요청]\n'; cat "$PROMPT_FILE")"
fi

truncate_output() {
  printf '%s' "$1" | head -c 2000
}

check_agy_review_log_dirs() {
  local log_root="${AGY_LOG_ROOT:-${HOME:-}/.gemini/antigravity-cli}"
  local directory
  for directory in "$log_root/log" "$log_root/crashes"; do
    # Missing directories are left to the provider, which may create them
    # itself. If they already exist, catch a definite permission/type error
    # before starting a review process that can only return logging noise.
    if [ -e "$directory" ] && { [ ! -d "$directory" ] || [ ! -w "$directory" ]; }; then
      printf '%s' "agy 로그 디렉터리에 쓰기 권한이 없거나 디렉터리가 아님: $directory"
      return 1
    fi
  done
  return 0
}

case "$TOOL" in
  codex)
    if [ ! -x "$CODEX_BIN" ]; then
      FAILURE_ENVELOPE "codex 실행파일을 찾을 수 없음: $CODEX_BIN (CODEX_BIN 환경변수로 경로를 override할 수 있음)"
      exit 0
    fi
    RAW_OUTPUT="$(EDGE_AGENT_PROVIDER_MODE=review "$PROVIDER_SANDBOX" "$CODEX_BIN" exec --skip-git-repo-check -s read-only "$PROMPT_CONTENT" 2>&1)"
    EXIT_CODE=$?
    ;;
  agy)
    if [ ! -x "$AGY_BIN" ]; then
      FAILURE_ENVELOPE "agy 실행파일을 찾을 수 없음: $AGY_BIN (AGY_BIN 환경변수로 경로를 override할 수 있음)"
      exit 0
    fi
    if ! AGY_LOG_PREFLIGHT="$(check_agy_review_log_dirs)"; then
      FAILURE_ENVELOPE "$AGY_LOG_PREFLIGHT"
      exit 0
    fi
    RAW_OUTPUT="$(env -u SSH_CONNECTION -u SSH_TTY -u SSH_CLIENT EDGE_AGENT_PROVIDER_MODE=review "$PROVIDER_SANDBOX" "$AGY_BIN" -p "$PROMPT_CONTENT" 2>&1)"
    EXIT_CODE=$?
    ;;
  *)
    FAILURE_ENVELOPE "알 수 없는 도구: $TOOL (codex 또는 agy만 지원)"
    exit 0
    ;;
esac

# Exit-status gate (fixed 2026-07-29 per grader feedback): a non-zero exit is
# always treated as a dispatch failure, full stop — we deliberately do NOT
# fall through to JSON extraction on the raw output even if it happens to
# contain something that parses as JSON. Rationale: on a crash/timeout/OOM/
# "argument list too long" (E2BIG on an oversized prompt) the captured
# stdout+stderr can easily contain an unrelated JSON-shaped fragment (a
# partial echo of the prompt itself, a JSON error body from the underlying
# API, etc.) that would otherwise be picked up by the extractor below and
# reported as a legitimate score — silently converting a real tool failure
# into a fabricated-looking success. Checking the exit code first closes
# that hole structurally, the same way argv-passing closes the injection
# hole: it doesn't rely on the extractor "happening" to reject the fragment.
if [ "$EXIT_CODE" -ne 0 ]; then
  FAILURE_ENVELOPE "도구가 비정상 종료(exit ${EXIT_CODE})함. 원본 출력(앞 2000자): $(truncate_output "$RAW_OUTPUT")"
  exit 0
fi

EXTRACTED="$(printf '%s' "$RAW_OUTPUT" | python3 -c '
import sys, json

def find_last_valid_json(text):
    # Tool CLIs (codex/agy) print banner/reasoning noise around the answer,
    # sometimes wrapped in markdown code fences, sometimes pretty-printed
    # across multiple lines. Brace-match to find balanced candidates,
    # validate each with json.loads, and keep the last (i.e. rightmost /
    # final) one that actually parses.
    #
    # Bug fixed here (found 2026-07-29 via two independent verify-task
    # graders returning bare {"scores":{...}} with no total/dealbreaker/
    # feedback wrapper): the previous version scanned EVERY "{" as a
    # candidate start, including ones nested inside an already-matched
    # outer object. For an envelope like {"scores": {...}, "total": 0,
    # "dealbreaker": ..., "feedback": "..."} the outer object starts at
    # position 0, but the nested "scores" sub-object necessarily starts at
    # a LARGER position. Both are valid, independently-parseable JSON, so
    # both landed in `candidates` — and since candidates were appended in
    # start-position order, candidates[-1] was actually the *inner*
    # sub-object, not the outer envelope we meant by "last/final". A tool
    # that answered correctly with the full envelope would still have its
    # wrapper fields silently stripped by this extractor.
    #
    # Fix: only ever consider top-level (non-nested) balanced-brace spans
    # as candidates. Once a matched closing brace is found, resume
    # scanning immediately after it, so nothing inside that span is ever
    # examined as a start position. This also makes the matcher
    # string-aware (braces inside quoted JSON string values, e.g. inside a
    # "feedback" string, no longer perturb the depth count).
    candidates = []
    n = len(text)
    start = 0
    while start < n:
        if text[start] != "{":
            start += 1
            continue
        depth = 0
        in_string = False
        escape = False
        end_found = None
        for end in range(start, n):
            ch = text[end]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == "\"":
                    in_string = False
                continue
            if ch == "\"":
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_found = end
                    break
        if end_found is not None:
            candidate = text[start:end_found + 1]
            try:
                json.loads(candidate)
                candidates.append(candidate)
            except Exception:
                pass
            start = end_found + 1
        else:
            start += 1
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
  FAILURE_ENVELOPE "도구 출력에서 유효한 JSON을 찾지 못함. 원본 출력(앞 2000자): $(truncate_output "$RAW_OUTPUT")"
fi
