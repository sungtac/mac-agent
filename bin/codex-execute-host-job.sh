#!/usr/bin/env bash
# Host-side Codex execution for calls originating inside Claude's sandbox.
# launchctl submit starts this outside the caller's nested seatbelt.
set -uo pipefail

CWD="$1"
PROMPT_FILE="$2"
OUTPUT_FILE="$3"
STATUS_FILE="$4"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
. "$SCRIPT_DIR/../workflows/lib/provider-bin.sh"

write_failure() {
  local code="$1"
  local message="$2"
  printf '%s\n' "$message" > "$OUTPUT_FILE"
  printf '%s\n' "$code" > "$STATUS_FILE"
  exit 0
}

# Validate on the host side too. A literal mktemp template or stale path must
# never turn into an empty Codex prompt that exits successfully.
if [ ! -f "$PROMPT_FILE" ]; then
  write_failure 66 "호스트 Codex 프롬프트 파일을 찾을 수 없음: $PROMPT_FILE"
fi
if [ ! -s "$PROMPT_FILE" ]; then
  write_failure 66 "호스트 Codex 프롬프트 파일이 비어 있음: $PROMPT_FILE"
fi

if ! /usr/bin/git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  write_failure 66 "작업 디렉토리가 git 저장소가 아님: $CWD"
fi
case "$CWD" in
  "$HOME/.openclaw"|"$HOME/.openclaw/"*|"$HOME/.edge-agent/retired-openclaw-workspace"|"$HOME/.edge-agent/retired-openclaw-workspace/"*)
    write_failure 77 "폐기된 OpenClaw 작업 디렉토리에서는 호스트 Codex를 실행하지 않음: $CWD"
    ;;
esac

CODEX_BIN="$(find_codex_bin || true)"
if [ -z "$CODEX_BIN" ] || [ ! -x "$CODEX_BIN" ]; then
  write_failure 69 "호스트 Codex 실행파일을 찾을 수 없음"
fi

CODEX_CWD="$CWD"
CODEX_PROMPT="$(cat "$PROMPT_FILE")"
case "$CODEX_CWD" in
  /private/tmp/*)
    CODEX_CWD="$(printf '%s' "$CODEX_CWD" | sed 's#^/private/tmp/#/tmp/#')"
    CODEX_PROMPT="$(printf '%s' "$CODEX_PROMPT" | sed 's#/private/tmp/#/tmp/#g')"
    ;;
esac

"$CODEX_BIN" exec -s workspace-write -C "$CODEX_CWD" "$CODEX_PROMPT" >"$OUTPUT_FILE" 2>&1
EXIT_CODE=$?
printf '%s\n' "$EXIT_CODE" > "$STATUS_FILE"
exit 0
