#!/usr/bin/env bash
set -euo pipefail

# Installs no service by default. Use --install only after the operations
# preflight (including explicit --allow-execute) and external TLS/Webhook
# endpoint have been reviewed.

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${HOME:?HOME is required}/Library/LaunchAgents"
REPLACE=0
INSTALL=0

usage() {
  printf '%s\n' 'usage: install-code-review-launchd.sh [--dry-run] [--install] [--replace]'
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) INSTALL=0 ;;
    --install) INSTALL=1 ;;
    --replace) REPLACE=1 ;;
    --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

WORKER_PLIST="$ROOT_DIR/config/com.macagent.code-review-worker.plist.template"
WEBHOOK_PLIST="$ROOT_DIR/config/com.macagent.code-review-webhook-server.plist.template"
WORKER_TARGET="$TARGET_DIR/com.macagent.code-review-worker.plist"
WEBHOOK_TARGET="$TARGET_DIR/com.macagent.code-review-webhook-server.plist"
SECRET_FILE="${HOME}/.edge-agent/secrets/code-review-webhook.secret"

/usr/bin/plutil -lint "$WORKER_PLIST" >/dev/null
/usr/bin/plutil -lint "$WEBHOOK_PLIST" >/dev/null
if [ ! -f "$SECRET_FILE" ]; then
  echo "secret file is missing: $SECRET_FILE" >&2
  exit 3
fi
SECRET_MODE="$(/usr/bin/stat -f '%Lp' "$SECRET_FILE")"
if [ "$SECRET_MODE" != "600" ]; then
  echo "secret file must have mode 600: $SECRET_FILE" >&2
  exit 3
fi

if [ "$INSTALL" -eq 0 ]; then
  printf '%s\n' "dry-run: would install $WORKER_TARGET"
  printf '%s\n' "dry-run: would install $WEBHOOK_TARGET"
  printf '%s\n' 'dry-run: would bootstrap both LaunchAgents'
  exit 0
fi

if [ "$REPLACE" -eq 0 ] && { [ -e "$WORKER_TARGET" ] || [ -e "$WEBHOOK_TARGET" ]; }; then
  echo 'LaunchAgent target already exists; use --replace explicitly' >&2
  exit 4
fi

/bin/mkdir -p "$TARGET_DIR" "${HOME}/.edge-agent/logs"
/bin/cp "$WORKER_PLIST" "$WORKER_TARGET"
/bin/cp "$WEBHOOK_PLIST" "$WEBHOOK_TARGET"
/usr/bin/plutil -lint "$WORKER_TARGET" >/dev/null
/usr/bin/plutil -lint "$WEBHOOK_TARGET" >/dev/null
/bin/launchctl bootstrap "gui/$(/usr/bin/id -u)" "$WORKER_TARGET"
/bin/launchctl bootstrap "gui/$(/usr/bin/id -u)" "$WEBHOOK_TARGET"
printf '%s\n' 'installed and bootstrapped code-review LaunchAgents'
