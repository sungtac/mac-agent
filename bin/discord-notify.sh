#!/usr/bin/env bash
# Legacy compatibility shim for the retired Discord notification channel.
# It intentionally does not send messages after Discord retirement.
# The old implementation posted a one-off message to the configured Discord channel via the bot's
# REST API (no Gateway connection needed — this doesn't require the
# discord-bot.py process to be running). Used by other scripts
# (weekly-report.sh, work-log-stop-check.sh, verify-task-orchestrator.py) to push
# escalation/failure notifications one-way, Mac → Discord.
#
# Usage: discord-notify.sh "message text"
#
# On success, prints the posted message's Discord id to stdout (Phase 2) so
# a calling script can record a pending-job file keyed by that id and let
# discord-bot.py recognize a reply to it later. Prints nothing on failure —
# callers must treat an empty stdout as "no id, don't expect a reply".
#
# Deliberately never fails the caller: config missing, malformed, or the
# Discord API call itself failing are all logged to stderr and exit 0 — a
# notification side-channel going down should never take the primary
# automation down with it.
set -uo pipefail

CONFIG="$HOME/.claude/discord-bot/config.json"
MESSAGE="${1:?usage: discord-notify.sh <message>}"

echo "discord-notify: Discord is retired; notification suppressed" >&2
exit 0

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

RESPONSE="$(curl -s --max-time 10 -X POST \
  "https://discord.com/api/v10/channels/${CHANNEL_ID}/messages" \
  -H "Authorization: Bot ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$BODY")"

MESSAGE_ID="$(python3 -c "import json,sys
try:
    print(json.loads(sys.argv[1])['id'])
except Exception:
    pass" "$RESPONSE" 2>/dev/null)"

if [ -z "$MESSAGE_ID" ]; then
  echo "discord-notify: Discord API call failed or returned no id: ${RESPONSE:0:200}" >&2
else
  echo "$MESSAGE_ID"
fi
exit 0
