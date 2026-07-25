#!/usr/bin/env bash
# Posts a one-off message to the configured Discord channel via the bot's
# REST API (no Gateway connection needed — this doesn't require the
# discord-bot.py process to be running). Used by other scripts
# (weekly-report.sh, work-log-stop-check.sh, verify-task-v2.js) to push
# escalation/failure notifications one-way, Mac → Discord.
#
# Usage: discord-notify.sh "message text"
#
# Deliberately never fails the caller: config missing, malformed, or the
# Discord API call itself failing are all logged to stderr and exit 0 — a
# notification side-channel going down should never take the primary
# automation down with it.
set -uo pipefail

CONFIG="$HOME/.claude/discord-bot/config.json"
MESSAGE="${1:?usage: discord-notify.sh <message>}"

if [ ! -f "$CONFIG" ]; then
  echo "discord-notify: config not found at $CONFIG, skipping" >&2
  exit 0
fi

TOKEN="$(python3 -c "import json,sys
try:
    print(json.load(open(sys.argv[1]))['token'])
except Exception:
    pass" "$CONFIG" 2>/dev/null)"
CHANNEL_ID="$(python3 -c "import json,sys
try:
    print(json.load(open(sys.argv[1]))['channel_id'])
except Exception:
    pass" "$CONFIG" 2>/dev/null)"

if [ -z "$TOKEN" ] || [ -z "$CHANNEL_ID" ]; then
  echo "discord-notify: token/channel_id missing or unparseable in $CONFIG, skipping" >&2
  exit 0
fi

BODY="$(python3 -c "import json,sys; print(json.dumps({'content': sys.argv[1][:2000]}))" "$MESSAGE" 2>/dev/null)"
if [ -z "$BODY" ]; then
  echo "discord-notify: failed to build request body, skipping" >&2
  exit 0
fi

HTTP_CODE="$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' -X POST \
  "https://discord.com/api/v10/channels/${CHANNEL_ID}/messages" \
  -H "Authorization: Bot ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$BODY")"

if [ "$HTTP_CODE" != "200" ] && [ "$HTTP_CODE" != "201" ]; then
  echo "discord-notify: Discord API returned HTTP ${HTTP_CODE}" >&2
fi
exit 0
