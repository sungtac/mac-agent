#!/usr/bin/env bash
# Fleet agent setup: checks Codex CLI / Antigravity CLI (agy) login status,
# and guides the user through logging in when needed.
#
# OAuth logins can't be fully silent — a human has to approve in a browser —
# so this script detects whether it's running in a real interactive terminal
# and either drives the login directly, or tells you the exact command to run
# yourself in one.
set -uo pipefail

is_tty() { [ -t 0 ] && [ -t 1 ]; }

section() { printf '\n=== %s ===\n' "$1"; }

section "Claude Code"
if command -v claude >/dev/null 2>&1; then
  echo "claude CLI 발견됨 (이 스크립트를 Claude Code로 실행 중이라면 이미 로그인된 상태)."
else
  echo "claude CLI를 찾을 수 없음. https://claude.com/claude-code 에서 설치 필요."
fi

section "Codex CLI"
if ! command -v codex >/dev/null 2>&1; then
  echo "codex CLI가 설치되어 있지 않음."
  echo "설치: npm install -g @openai/codex  (또는 brew install codex)"
else
  if codex login status >/dev/null 2>&1; then
    echo "이미 로그인됨: $(codex login status 2>&1)"
  else
    echo "로그인이 필요합니다."
    if is_tty; then
      echo "지금 로그인을 진행합니다..."
      codex login
    else
      echo "인터랙티브 터미널이 아니라 자동으로 로그인할 수 없습니다."
      echo "직접 터미널을 열어서 아래 명령을 실행해주세요:"
      echo "  codex login"
    fi
  fi
fi

section "Antigravity CLI (agy)"
AGY_BIN="$(command -v agy 2>/dev/null || true)"
if [ -z "$AGY_BIN" ] && [ -x "$HOME/.local/bin/agy" ]; then
  AGY_BIN="$HOME/.local/bin/agy"
fi
if [ -z "$AGY_BIN" ]; then
  echo "agy를 찾을 수 없음."
  echo "Antigravity 앱 설치 필요: https://antigravity.google (설치 후 agy가 ~/.local/bin/agy 에 생성됨)"
else
  if "$AGY_BIN" models >/dev/null 2>&1; then
    echo "이미 로그인됨."
  else
    echo "로그인이 필요합니다."
    if is_tty; then
      echo "지금 로그인을 진행합니다... (URL이 뜨면 브라우저에서 열고, 표시되는 인증 코드를 여기에 붙여넣으세요)"
      env -u SSH_CONNECTION -u SSH_TTY -u SSH_CLIENT "$AGY_BIN"
    else
      echo "인터랙티브 터미널이 아니라 자동으로 로그인할 수 없습니다."
      echo "직접 터미널을 열어서 아래 명령을 실행해주세요:"
      echo "  agy"
      echo "(SSH로 접속 중이면 브라우저 대신 URL+인증코드 붙여넣기 방식으로 진행됩니다. 60초 제한이 있으니 빠르게 진행하세요.)"
    fi
  fi
fi

section "요약"
echo "위 상태를 확인하고, 로그인이 필요하다고 나온 도구는 안내된 명령을 실행해주세요."
echo "다시 확인하려면: bash setup.sh"
