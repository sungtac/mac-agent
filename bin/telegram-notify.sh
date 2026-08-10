#!/usr/bin/env bash
# One-way ops notification: Mac -> Telegram "claude" bot chat, replacing the
# retired discord-notify.sh (Discord channel was retired; that script is now
# a no-op shim). Used by cron/kakao-morning-briefing.sh, cron/weekly-report.sh,
# and cron/token-cost-report.sh to push failure/escalation notices.
#
# Reuses the same bot/chat the always-on telegram-claude launchd agent uses
# (~/Library/LaunchAgents/com.macagent.telegram-claude.plist), so operator
# already has this chat open. This is a plain REST call, independent of
# telegram-agent-bot.py's own process being up.
#
# Usage: telegram-notify.sh "message text"
#
# On success, prints the sent message's Telegram message_id to stdout (same
# contract discord-notify.sh had) so a caller can record a pending-job file
# keyed by that id. Note: unlike the old Discord setup, nothing currently
# consumes that id on the Telegram side (discord-bot.py's reply-tracking was
# Discord-specific and is retired along with Discord) — treat this as
# one-way-only for now.
#
# Deliberately never fails the caller: token file missing/unreadable or the
# Telegram API call itself failing are both logged to stderr and exit 0 — a
# notification side-channel going down should never take the primary
# automation down with it.
set -uo pipefail

TOKEN_FILE="${TELEGRAM_NOTIFY_TOKEN_FILE:-$HOME/.edge-agent/secrets/telegram/claude.token}"
CHAT_ID="${TELEGRAM_NOTIFY_CHAT_ID:--1003952617795}"
MESSAGE="${1:?usage: telegram-notify.sh <message>}"

if [ ! -f "$TOKEN_FILE" ]; then
  echo "telegram-notify: token file not found at $TOKEN_FILE, skipping" >&2
  exit 0
fi

TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
if [ -z "$TOKEN" ]; then
  echo "telegram-notify: token file at $TOKEN_FILE is empty, skipping" >&2
  exit 0
fi

RESPONSE="$(curl -s --max-time 10 -X POST \
  "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c "import json,sys; print(json.dumps({'chat_id': sys.argv[1], 'text': sys.argv[2][:4000]}))" "$CHAT_ID" "$MESSAGE" 2>/dev/null)")"

MESSAGE_ID="$(python3 -c "import json,sys
try:
    print(json.loads(sys.argv[1])['result']['message_id'])
except Exception:
    pass" "$RESPONSE" 2>/dev/null)"

if [ -z "$MESSAGE_ID" ]; then
  echo "telegram-notify: Telegram API call failed or returned no message_id: ${RESPONSE:0:200}" >&2
else
  echo "$MESSAGE_ID"
fi
exit 0
