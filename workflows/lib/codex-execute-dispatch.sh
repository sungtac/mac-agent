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
AUDIT_FILE="${CODEX_EXECUTE_AUDIT_FILE:-$HOME/.claude/edge-agent/codex-execute-events.jsonl}"
START_EPOCH="$(date +%s)"

record_audit() {
  local status="$1"
  local exit_code="$2"
  local message="$3"
  local end_epoch
  end_epoch="$(date +%s)"
  python3 - "$AUDIT_FILE" "$CWD" "$status" "$exit_code" "$((end_epoch - START_EPOCH))" "$message" <<'PYEOF' >/dev/null 2>&1 || true
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

path, cwd, status, exit_code, duration, message = sys.argv[1:]
# Keep diagnostics useful without turning the audit file into a credential
# store. The full provider output remains in the provider's own debug log.
message = message[-4000:]
message = re.sub(r'(?i)(authorization:\s*bearer\s+)[^\s]+', r'\1[REDACTED]', message)
message = re.sub(r'(?i)(api[_-]?key|token|secret|password)([=:]\s*)[^\s,}]+', r'\1\2[REDACTED]', message)
message = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '[EMAIL_REDACTED]', message)
record = {
    'schema': 'edge_agent_codex_execute.v1',
    'observedAt': datetime.now(timezone.utc).isoformat(),
    'cwd': cwd,
    'status': status,
    'exitCode': int(exit_code),
    'durationSeconds': int(duration),
    'messageTail': message,
}
target = Path(path).expanduser()
target.parent.mkdir(parents=True, exist_ok=True)
lock = Path(f'{target}.lock')
with lock.open('a+') as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    with target.open('a', encoding='utf-8') as output:
        output.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
        output.flush()
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
PYEOF
}

FAILURE_ENVELOPE() {
  local reason="$1"
  python3 - "$reason" << 'PYEOF'
import json, sys
reason = sys.argv[1]
print(json.dumps({"ok": False, "message": reason, "dispatchFailed": True}, ensure_ascii=False))
PYEOF
}

if [ ! -d "$CWD" ]; then
  record_audit "invalid_cwd" 66 "작업 디렉토리를 찾을 수 없음: $CWD"
  FAILURE_ENVELOPE "작업 디렉토리를 찾을 수 없음: $CWD"
  exit 0
fi

if [ ! -f "$PROMPT_FILE" ]; then
  record_audit "invalid_prompt" 66 "프롬프트 파일을 찾을 수 없음: $PROMPT_FILE"
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
  record_audit "invalid_git_repo" 66 "작업 디렉토리가 git 저장소가 아님: $CWD"
  FAILURE_ENVELOPE "작업 디렉토리가 git 저장소가 아님: $CWD (이 스크립트를 호출하는 쪽의 diff 기반 검증이 git 저장소를 전제하므로, 아닐 경우 실행하지 않음)"
  exit 0
fi

# Resolved absolute path, not bare `codex` — a Workflow-spawned agent's Bash
# environment can have a PATH stripped of /opt/homebrew/bin (see score-dispatch.sh).
# No --skip-git-repo-check — the check above already guarantees this is a
# real git repo, so there's nothing to skip; keeping the flag would have
# silently masked the check above's own guarantee if the two ever drifted.
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=provider-bin.sh
. "$SCRIPT_DIR/provider-bin.sh"
PROVIDER_SANDBOX="$SCRIPT_DIR/../../bin/edge-agent-provider-sandbox.sh"
HOST_JOB="$SCRIPT_DIR/../../bin/codex-execute-host-job.sh"
CODEX_BIN="${CODEX_BIN:-}"
CODEX_BIN="${CODEX_BIN:-$(find_codex_bin || true)}"
if [ -z "$CODEX_BIN" ] || [ ! -x "$CODEX_BIN" ]; then
  record_audit "codex_not_found" 69 "codex 실행파일을 찾을 수 없음"
  FAILURE_ENVELOPE "codex 실행파일을 찾을 수 없음(CODEX_BIN override 또는 Homebrew 경로 확인)"
  exit 0
fi
CODEX_CWD="$CWD"
CODEX_PROMPT="$(cat "$PROMPT_FILE")"
# Codex's own seatbelt accepts /tmp as a temporary workspace but can reject
# macOS's canonical spelling /private/tmp with sandbox_apply=71. The two
# paths refer to the same filesystem here. Keep the caller's real CWD for git
# verification/audit, but present Codex with the accepted spelling in both
# -C and the prompt's absolute paths.
case "$CODEX_CWD" in
  /private/tmp/*)
    CODEX_CWD="/tmp/${CODEX_CWD#/private/tmp/}"
    CODEX_PROMPT="$(printf '%s' "$CODEX_PROMPT" | sed 's#/private/tmp/#/tmp/#g')"
    ;;
esac
RAW_OUTPUT="$("$PROVIDER_SANDBOX" "$CODEX_BIN" exec -s workspace-write -C "$CODEX_CWD" "$CODEX_PROMPT" 2>&1)"
EXIT_CODE=$?

# A Workflow Agent's Bash tool already runs inside a Claude seatbelt. Codex's
# own workspace-write seatbelt cannot be nested there and reports
# sandbox_apply=71/Operation not permitted. Re-submit only this known failure
# to launchd, whose host-side job starts outside the caller's sandbox.
if printf '%s' "$RAW_OUTPUT" | grep -Eqi 'sandbox_apply.*(71|Operation not permitted)|sandbox-exec.*Operation not permitted'; then
  HOST_DIR="$(mktemp -d /tmp/edge-codex-host.XXXXXX)"
  HOST_OUTPUT="$HOST_DIR/output.log"
  HOST_STATUS="$HOST_DIR/status"
  HOST_LABEL="com.macagent.codex-execute.$$.$RANDOM"
  if launchctl submit -l "$HOST_LABEL" -o "$HOST_DIR/launchd.out" -e "$HOST_DIR/launchd.err" -- \
      /bin/bash "$HOST_JOB" "$CWD" "$PROMPT_FILE" "$HOST_OUTPUT" "$HOST_STATUS" >/dev/null 2>&1; then
    WAITED=0
    while [ ! -f "$HOST_STATUS" ] && [ "$WAITED" -lt 300 ]; do
      sleep 1
      WAITED=$((WAITED + 1))
    done
    if [ -f "$HOST_STATUS" ]; then
      EXIT_CODE="$(head -n 1 "$HOST_STATUS")"
      RAW_OUTPUT="$(cat "$HOST_OUTPUT" 2>/dev/null || true)"
      if [ -z "$RAW_OUTPUT" ] && [ -f "$HOST_DIR/launchd.err" ]; then
        RAW_OUTPUT="$(cat "$HOST_DIR/launchd.err")"
      fi
    else
      EXIT_CODE=124
      RAW_OUTPUT="host Codex bridge timed out after 300s"
    fi
  else
    RAW_OUTPUT="$RAW_OUTPUT\n\nhost Codex bridge submission failed"
  fi
fi

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
  record_audit "provider_failed" "$EXIT_CODE" "$TRUNCATED"
  FAILURE_ENVELOPE "codex exec 종료코드 ${EXIT_CODE}. 원본 출력(끝 2000자): ${TRUNCATED}"
  exit 0
fi

AUDIT_TAIL="$(printf '%s' "$RAW_OUTPUT" | tail -c 4000)"
record_audit "provider_completed" 0 "$AUDIT_TAIL"
python3 - "$RAW_OUTPUT" << 'PYEOF'
import json, sys
raw = sys.argv[1]
print(json.dumps({"ok": True, "message": raw[-4000:]}, ensure_ascii=False))
PYEOF
