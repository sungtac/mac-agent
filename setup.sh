#!/usr/bin/env bash
# Fleet agent setup: checks Codex CLI / Antigravity CLI (agy) login status,
# installs them via Homebrew if missing, and guides the user through logging
# in when needed.
#
# OAuth logins can't be fully silent — a human has to approve in a browser —
# so this script auto-installs what it safely can (Homebrew casks) and either
# drives the login directly (real interactive terminal) or tells you the
# exact command to run yourself (SSH/non-interactive).
set -uo pipefail

is_tty() { [ -t 0 ] && [ -t 1 ]; }

section() { printf '\n=== %s ===\n' "$1"; }

confirm() {
  local prompt="$1"
  if ! is_tty; then
    echo "인터랙티브 터미널이 아니라 설치 확인을 받을 수 없어 건너뜁니다."
    return 1
  fi
  read -r -p "$prompt [y/N] " reply
  case "$reply" in
    [yY]|[yY][eE][sS]) return 0 ;;
    *) return 1 ;;
  esac
}

install_cask() {
  local cask="$1"
  local desc="$2"
  if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew가 없어서 자동 설치할 수 없습니다. https://brew.sh 에서 먼저 설치해주세요."
    return 1
  fi
  if ! confirm "brew install --cask $cask 를 실행해서 설치할까요? ($desc)"; then
    echo "설치를 건너뜁니다. 나중에 수동 설치: brew install --cask $cask"
    return 1
  fi
  echo "brew install --cask $cask 실행 중..."
  brew install --cask "$cask"
}

section "Claude Code"
if command -v claude >/dev/null 2>&1; then
  echo "claude CLI 발견됨 (이 스크립트를 Claude Code로 실행 중이라면 이미 로그인된 상태)."
else
  echo "claude CLI를 찾을 수 없음. https://claude.com/claude-code 에서 설치 필요."
fi

section "Codex CLI"
if ! command -v codex >/dev/null 2>&1; then
  echo "codex CLI가 설치되어 있지 않음."
  install_cask codex "CLI 전용 도구라 Applications/Launchpad에 아이콘은 생기지 않고, 터미널 명령어만 추가됨"
fi
if command -v codex >/dev/null 2>&1; then
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
else
  echo "설치 실패 또는 설치 안 됨. 수동 설치: brew install --cask codex"
fi

section "Antigravity CLI (agy)"
AGY_BIN="$(command -v agy 2>/dev/null || true)"
if [ -z "$AGY_BIN" ] && [ -x "$HOME/.local/bin/agy" ]; then
  AGY_BIN="$HOME/.local/bin/agy"
fi
if [ -z "$AGY_BIN" ]; then
  echo "agy를 찾을 수 없음."
  install_cask antigravity "Antigravity.app이 Applications 폴더/Launchpad에 실제 앱 아이콘으로 설치됨"
  AGY_BIN="$(command -v agy 2>/dev/null || true)"
  if [ -z "$AGY_BIN" ] && [ -x "$HOME/.local/bin/agy" ]; then
    AGY_BIN="$HOME/.local/bin/agy"
  fi
fi
if [ -z "$AGY_BIN" ]; then
  echo "설치 실패 또는 설치 안 됨. 수동 설치: brew install --cask antigravity"
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
