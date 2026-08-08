#!/usr/bin/env bash
# PostToolUse(Edit|Write) companion to the dotfile fast-path in
# verify-task-pre-edit-gate.sh (2026-08-08, hardened 2026-08-08b).
#
# Only acts on the same small allowlist of personal shell dotfiles under
# $HOME. A plain `zsh -n` / `bash -n` syntax check is NOT enough: some
# malformed constructs (e.g. an unmatched paren inside a brace-less function
# body) pass -n cleanly and only surface as a non-fatal stderr warning when
# the file is actually sourced, with the shell still exiting 0 (verified
# empirically 2026-08-08 after a real incident where the syntax-only check
# missed a broken .zshenv). So this check ACTUALLY SOURCES the file in a
# throwaway shell, fully isolated from the user's real environment
# (`env -i` + `-f`/`--norc --noprofile`, so it never re-reads the user's
# other dotfiles or inherits their session state) and treats ANY stderr
# output as a failure, regardless of exit code. A background watchdog kills
# the shell if sourcing hangs (e.g. an accidental infinite loop), since this
# host has no `timeout`/`gtimeout` binary.

set -uo pipefail

INPUT="$(cat)"
FILE_PATH="$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
[ -n "$FILE_PATH" ] || exit 0

BASENAME="$(basename "$FILE_PATH")"
case "$BASENAME" in
  .zshenv|.zshrc|.zprofile) SHELL_BIN="/bin/zsh"; SHELL_ISOLATE_ARGS=(-f) ;;
  .bashrc|.bash_profile)   SHELL_BIN="/bin/bash"; SHELL_ISOLATE_ARGS=(--norc --noprofile) ;;
  *) exit 0 ;;
esac

DOTFILE_FASTPATH_RE='^'"${HOME}"'/\.(zshenv|zshrc|zprofile|bashrc|bash_profile)$'
[[ "$FILE_PATH" =~ $DOTFILE_FASTPATH_RE ]] || exit 0
[ -f "$FILE_PATH" ] || exit 0

ERR_FILE="$(mktemp "${TMPDIR:-/tmp}/dotfile-fastpath-check.XXXXXX")"
trap 'rm -f "$ERR_FILE"' EXIT

# Actually source the file in an isolated throwaway shell (env -i + -f /
# --norc --noprofile so it never touches the user's other dotfiles or real
# session), with a 5s watchdog kill for accidental infinite loops. Both the
# job and its watchdog are disowned/silenced so a killed watchdog does not
# print a stray "Terminated" job-control notice from this script.
env -i HOME="$HOME" "$SHELL_BIN" "${SHELL_ISOLATE_ARGS[@]}" -c "source '$FILE_PATH'" \
  >/dev/null 2>"$ERR_FILE" &
SOURCE_PID=$!
( sleep 5; kill -9 "$SOURCE_PID" 2>/dev/null ) >/dev/null 2>&1 &
WATCHDOG_PID=$!
disown "$WATCHDOG_PID" 2>/dev/null
wait "$SOURCE_PID" 2>/dev/null
kill "$WATCHDOG_PID" >/dev/null 2>&1

ERR="$(cat "$ERR_FILE" 2>/dev/null)"
[ -z "$ERR" ] && exit 0

BACKUP_DIR="$HOME/.claude/hooks-state/dotfile-fastpath-backups"
LATEST_BACKUP="$(ls -t "$BACKUP_DIR/${BASENAME}".*.bak 2>/dev/null | head -1)"

if [ -n "$LATEST_BACKUP" ]; then
  cp -p "$LATEST_BACKUP" "$FILE_PATH"
  RESTORE_MSG="오류가 있어 이전 백업($LATEST_BACKUP)으로 자동 복구했습니다."
else
  RESTORE_MSG="오류가 있는데 백업을 찾지 못해 자동 복구하지 못했습니다 — 파일을 직접 확인하세요."
fi

jq -n --arg file "$FILE_PATH" --arg err "$ERR" --arg restore "$RESTORE_MSG" '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    additionalContext: ("[dotfile fast-path 검증 실패] " + $file + " 로드 시 오류: " + $err + " " + $restore)
  }
}'
