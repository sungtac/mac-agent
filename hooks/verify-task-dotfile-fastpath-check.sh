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
# Also validates the same fast-path's Claude JSON settings and project memory
# Markdown files, restoring the latest backup when validation fails.

set -uo pipefail

INPUT="$(cat)"
FILE_PATH="$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
[ -n "$FILE_PATH" ] || exit 0

CLAUDE_SETTINGS_JSON_FASTPATH_RE='^'"${HOME}"'/\.claude/settings\.json$'
CLAUDE_SETTINGS_LOCAL_JSON_FASTPATH_RE='^'"${HOME}"'/\.claude/settings\.local\.json$'
CLAUDE_MEMORY_FASTPATH_RE='^'"${HOME}"'/\.claude/projects/-Users-edge-ai--claude/memory/[^/]+\.md$'

report_fastpath_failure() {
  local kind="$1"
  local err="$2"
  local backup_dir="$HOME/.claude/hooks-state/dotfile-fastpath-backups"
  local basename latest_backup restore_msg

  basename="$(basename "$FILE_PATH")"
  latest_backup="$(ls -t "$backup_dir/${basename}".*.bak 2>/dev/null | head -1)"

  if [ -n "$latest_backup" ]; then
    if cp -p "$latest_backup" "$FILE_PATH" 2>/dev/null; then
      restore_msg="오류가 있어 이전 백업($latest_backup)으로 자동 복구했습니다."
    else
      restore_msg="오류가 있어 백업 복구를 시도했지만 실패했습니다 — 파일을 직접 확인하세요."
    fi
  else
    restore_msg="오류가 있는데 백업을 찾지 못해 자동 복구하지 못했습니다 — 파일을 직접 확인하세요."
  fi

  jq -n --arg kind "$kind" --arg file "$FILE_PATH" --arg err "$err" --arg restore "$restore_msg" '{
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      additionalContext: ("[" + $kind + " fast-path 검증 실패] " + $file + " 검증 오류: " + $err + " " + $restore)
    }
  }'
}

if [[ "$FILE_PATH" =~ $CLAUDE_SETTINGS_JSON_FASTPATH_RE ]] || [[ "$FILE_PATH" =~ $CLAUDE_SETTINGS_LOCAL_JSON_FASTPATH_RE ]]; then
  if git -C "$HOME" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    exit 0
  fi
  [ -f "$FILE_PATH" ] || exit 0

  ERR_FILE="$(mktemp "${TMPDIR:-/tmp}/json-fastpath-check.XXXXXX")"
  trap 'rm -f "$ERR_FILE"' EXIT
  if jq empty "$FILE_PATH" 2>"$ERR_FILE"; then
    exit 0
  fi

  ERR="$(cat "$ERR_FILE" 2>/dev/null)"
  [ -n "$ERR" ] || ERR="유효한 JSON 문서가 아닙니다."
  report_fastpath_failure "JSON" "$ERR"
  exit 0
fi

if [[ "$FILE_PATH" =~ $CLAUDE_MEMORY_FASTPATH_RE ]]; then
  if git -C "$HOME" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    exit 0
  fi
  [ -f "$FILE_PATH" ] || exit 0

  ERR_FILE="$(mktemp "${TMPDIR:-/tmp}/memory-fastpath-check.XXXXXX")"
  trap 'rm -f "$ERR_FILE"' EXIT
  if [ "$(basename "$FILE_PATH")" = "MEMORY.md" ]; then
    if [ -s "$FILE_PATH" ]; then
      exit 0
    fi
    printf '%s\n' "MEMORY.md 파일이 비어 있습니다." >"$ERR_FILE"
  elif awk '
    NR == 1 {
      if ($0 != "---") {
        print "첫 줄이 ---가 아닙니다." > "/dev/stderr"
        invalid = 1
        exit 1
      }
      next
    }
    !closed && $0 == "---" {
      closed = 1
      next
    }
    !closed {
      if ($0 ~ /^[[:space:]]*name[[:space:]]*:/) name_key = 1
      if ($0 ~ /^[[:space:]]*description[[:space:]]*:/) description_key = 1
      if ($0 ~ /^[[:space:]]*metadata[[:space:]]*:/) metadata_key = 1
    }
    END {
      if (invalid) exit 1
      if (!closed) {
        print "frontmatter 종료 ---가 없습니다." > "/dev/stderr"
        exit 1
      }
      if (!name_key || !description_key || !metadata_key) {
        missing = ""
        if (!name_key) missing = missing " name:"
        if (!description_key) missing = missing " description:"
        if (!metadata_key) missing = missing " metadata:"
        print "frontmatter에 필수 키가 없습니다:" missing > "/dev/stderr"
        exit 1
      }
    }
  ' "$FILE_PATH" 2>"$ERR_FILE"; then
    exit 0
  fi

  ERR="$(cat "$ERR_FILE" 2>/dev/null)"
  [ -n "$ERR" ] || ERR="메모리 Markdown 구조가 올바르지 않습니다."
  report_fastpath_failure "memory Markdown" "$ERR"
  exit 0
fi

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
