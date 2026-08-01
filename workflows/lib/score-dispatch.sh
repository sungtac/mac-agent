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
# (its required fields). Optional, defaults to the rubric-compatible envelope
# for generic callers. verify-task-v2.js passes this explicitly. The
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

# rubric-compatible 계약: 정확히 이 필드와 dealbreaker_reason 문구를
# 유지한다. 일부 직접 호출자가 이 봉투 형식을 사용하므로 새 필드를
# 추가하지 않는다.
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

record_verify_metric() {
  [ -n "${VERIFY_METRICS_FILE:-}" ] || return 0
  python3 - "$VERIFY_METRICS_FILE" "$PROMPT_FILE" "${VERIFY_TASK_ID:-unknown}" "${VERIFY_AGENT:-unknown}" "${VERIFY_ROLE:-unknown}" <<'PYEOF' || true
import json
import os
import sys
from pathlib import Path

metrics_path, prompt_path, task_id, agent, role = sys.argv[1:]
try:
    package_bytes = os.path.getsize(prompt_path)
except OSError:
    package_bytes = 0
record = {
    "task_id": task_id,
    "track": None,
    "agent": agent,
    "role": role,
    "round": None,
    "model": None,
    "effort": None,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_creation_tokens": 0,
    "package_bytes": package_bytes,
    "package_tokens": max(1, package_bytes // 4),
    "prefix_fingerprint": None,
}
path = Path(metrics_path)
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
PYEOF
}

trap record_verify_metric EXIT

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

REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." 2>/dev/null && pwd -P)"
AGY_REVIEW_LOG_ROOT=""
AGY_REVIEW_PREFLIGHT_ERROR=""
AGY_REVIEW_PROFILE_TEMP=""
AGY_REVIEW_PROFILE_ERROR=""

resolve_agy_review_log_root() {
  local configured_root
  local normalized_root

  if [ "${AGY_LOG_ROOT+x}" = x ]; then
    configured_root="$AGY_LOG_ROOT"
    if [ -z "$configured_root" ]; then
      AGY_REVIEW_PREFLIGHT_ERROR="AGY_LOG_ROOT가 비어 있음"
      return 1
    fi
  else
    if [ -z "${HOME:-}" ]; then
      AGY_REVIEW_PREFLIGHT_ERROR="HOME이 비어 있어 Antigravity 로그 경로를 결정할 수 없음"
      return 1
    fi
    configured_root="$HOME/.gemini/antigravity-cli"
  fi

  if ! normalized_root="$(python3 -c '
import os
import sys

candidate, repository = sys.argv[1:]
if not os.path.isabs(candidate):
    raise SystemExit(1)
if any(ord(char) < 32 or ord(char) == 127 for char in candidate):
    raise SystemExit(1)
if os.path.islink(candidate):
    raise SystemExit(1)

normalized = os.path.realpath(candidate)
repository = os.path.realpath(repository)
try:
    inside_repository = os.path.commonpath((normalized, repository)) == repository
except ValueError:
    inside_repository = False
if inside_repository:
    raise SystemExit(1)
print(normalized)
' "$configured_root" "$REPO_ROOT")"; then
    AGY_REVIEW_PREFLIGHT_ERROR="Antigravity 로그 루트가 절대 경로가 아니거나 제어 문자를 포함하거나 심볼릭 링크이거나 저장소 내부임"
    return 1
  fi
  if [ -z "$normalized_root" ]; then
    AGY_REVIEW_PREFLIGHT_ERROR="Antigravity 로그 루트 정규화 결과가 비어 있음"
    return 1
  fi

  AGY_REVIEW_LOG_ROOT="$normalized_root"
  return 0
}

check_agy_review_log_dirs() {
  local log_root
  local directory

  if [ -z "$AGY_REVIEW_LOG_ROOT" ]; then
    AGY_REVIEW_PREFLIGHT_ERROR="Antigravity 로그 루트가 정해지지 않음"
    return 1
  fi
  log_root="$AGY_REVIEW_LOG_ROOT"

  if [ -e "$log_root" ] && [ ! -d "$log_root" ]; then
    AGY_REVIEW_PREFLIGHT_ERROR="agy 로그 루트가 디렉터리가 아님: $log_root"
    return 1
  fi
  for directory in "$log_root/log" "$log_root/crashes"; do
    # Missing directories are left to the provider, which may create them
    # itself. If they already exist, catch a definite permission/type error
    # before starting a review process that can only return logging noise.
    if [ -L "$directory" ] || { [ -e "$directory" ] && { [ ! -d "$directory" ] || [ ! -w "$directory" ]; }; }; then
      AGY_REVIEW_PREFLIGHT_ERROR="agy 로그 디렉터리에 쓰기 권한이 없거나 디렉터리가 아니거나 심볼릭 링크임: $directory"
      return 1
    fi
  done
  return 0
}

escape_seatbelt_path() {
  python3 - "$1" <<'PYEOF'
import sys

path = sys.argv[1]
if any(ord(char) < 32 or ord(char) == 127 for char in path):
    raise SystemExit(1)
print(path.replace('\\', '\\\\').replace('"', '\\"'))
PYEOF
}

cleanup_agy_review_profile() {
  local exit_status=$?
  if [ -n "$AGY_REVIEW_PROFILE_TEMP" ]; then
    rm -f -- "$AGY_REVIEW_PROFILE_TEMP" || true
  fi
  return "$exit_status"
}

create_agy_review_profile() {
  local base_profile
  local default_profile="$SCRIPT_DIR/../../config/code-review-read-only.sb"
  local escaped_log
  local escaped_crashes

  if [ "${EDGE_AGENT_REVIEW_PROFILE+x}" = x ]; then
    base_profile="$EDGE_AGENT_REVIEW_PROFILE"
  else
    base_profile="$default_profile"
  fi
  if [ -L "$base_profile" ] || [ ! -f "$base_profile" ] || [ ! -r "$base_profile" ]; then
    AGY_REVIEW_PROFILE_ERROR="Antigravity review 프로필이 없거나 읽을 수 없는 정규 파일이 아님: $base_profile"
    return 1
  fi

  if ! AGY_REVIEW_PROFILE_TEMP="$(mktemp "${TMPDIR:-/tmp}/edge-agent-review-profile.XXXXXX")"; then
    AGY_REVIEW_PROFILE_ERROR="Antigravity review 임시 프로필을 만들 수 없음"
    AGY_REVIEW_PROFILE_TEMP=""
    return 1
  fi
  if ! chmod 600 "$AGY_REVIEW_PROFILE_TEMP"; then
    AGY_REVIEW_PROFILE_ERROR="Antigravity review 임시 프로필 권한 설정 실패"
    return 1
  fi

  if ! python3 - "$base_profile" "$AGY_REVIEW_PROFILE_TEMP" <<'PYEOF'
import shutil
import sys

shutil.copyfile(sys.argv[1], sys.argv[2])
PYEOF
  then
    AGY_REVIEW_PROFILE_ERROR="Antigravity review 기본 프로필 복사 실패: $base_profile"
    return 1
  fi

  if [ "${EDGE_AGENT_REVIEW_PROFILE+x}" != x ] && ! python3 - "$AGY_REVIEW_PROFILE_TEMP" <<'PYEOF'
import sys

profile_path = sys.argv[1]
with open(profile_path, "rb") as profile:
    lines = profile.readlines()
with open(profile_path, "wb") as profile:
    for line in lines:
        stripped = line.strip()
        if (stripped.startswith(b'(allow file-write* (subpath "')
                and (b'/antigravity-cli/log' in stripped or b'/antigravity-cli/crashes' in stripped)):
            continue
        profile.write(line)
PYEOF
  then
    AGY_REVIEW_PROFILE_ERROR="기본 Antigravity review 프로필의 고정 로그 허용 규칙 제거 실패"
    return 1
  fi

  if ! chmod 600 "$AGY_REVIEW_PROFILE_TEMP"; then
    AGY_REVIEW_PROFILE_ERROR="Antigravity review 임시 프로필 권한 재설정 실패"
    return 1
  fi
  if ! escaped_log="$(escape_seatbelt_path "$AGY_REVIEW_LOG_ROOT/log")"; then
    AGY_REVIEW_PROFILE_ERROR="Antigravity 로그 경로를 sandbox 프로필 문자열로 변환할 수 없음"
    return 1
  fi
  if ! escaped_crashes="$(escape_seatbelt_path "$AGY_REVIEW_LOG_ROOT/crashes")"; then
    AGY_REVIEW_PROFILE_ERROR="Antigravity crash 경로를 sandbox 프로필 문자열로 변환할 수 없음"
    return 1
  fi
  if ! {
    printf '%s\n' "(allow file-write* (subpath \"$escaped_log\"))"
    printf '%s\n' "(allow file-write* (subpath \"$escaped_crashes\"))"
  } >> "$AGY_REVIEW_PROFILE_TEMP"; then
    AGY_REVIEW_PROFILE_ERROR="Antigravity review 동적 로그 허용 규칙 추가 실패"
    return 1
  fi
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
    if ! resolve_agy_review_log_root; then
      FAILURE_ENVELOPE "$AGY_REVIEW_PREFLIGHT_ERROR"
      exit 0
    fi
    if ! check_agy_review_log_dirs; then
      FAILURE_ENVELOPE "$AGY_REVIEW_PREFLIGHT_ERROR"
      exit 0
    fi
    trap cleanup_agy_review_profile EXIT
    if ! create_agy_review_profile; then
      FAILURE_ENVELOPE "$AGY_REVIEW_PROFILE_ERROR"
      exit 0
    fi
    RAW_OUTPUT="$(env -u SSH_CONNECTION -u SSH_TTY -u SSH_CLIENT AGY_LOG_ROOT="$AGY_REVIEW_LOG_ROOT" EDGE_AGENT_REVIEW_PROFILE="$AGY_REVIEW_PROFILE_TEMP" EDGE_AGENT_PROVIDER_MODE=review "$PROVIDER_SANDBOX" "$AGY_BIN" -p "$PROMPT_CONTENT" 2>&1)"
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
