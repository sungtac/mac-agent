#!/usr/bin/env bash

# launchd/Workflow 환경은 PATH가 축소될 수 있고, Intel/Apple Silicon Mac의
# Homebrew 경로도 다르다. 호출부는 이 함수의 결과가 빈 문자열이면 명확한
# "실행파일 없음" 실패를 반환해야 한다.

find_codex_bin() {
  if [ -n "${CODEX_BIN:-}" ] && [ -x "$CODEX_BIN" ]; then
    printf '%s\n' "$CODEX_BIN"
    return 0
  fi
  local candidate
  for candidate in "${HOME:-}/.local/bin/codex" /opt/homebrew/bin/codex /usr/local/bin/codex; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  command -v codex 2>/dev/null || return 1
}

find_agy_bin() {
  if [ -n "${AGY_BIN:-}" ] && [ -x "$AGY_BIN" ]; then
    printf '%s\n' "$AGY_BIN"
    return 0
  fi
  local candidate
  for candidate in "${HOME:-}/.local/bin/agy" /opt/homebrew/bin/agy /usr/local/bin/agy; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  command -v agy 2>/dev/null || return 1
}

find_claude_bin() {
  if [ -n "${CLAUDE_BIN:-}" ] && [ -x "$CLAUDE_BIN" ]; then
    printf '%s\n' "$CLAUDE_BIN"
    return 0
  fi
  local candidate
  for candidate in "${HOME:-}/.local/bin/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  command -v claude 2>/dev/null || return 1
}
