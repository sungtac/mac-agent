#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_HOME="$(mktemp -d "${TMPDIR:-/tmp}/edge-agent-advisor-test.XXXXXX")"
trap 'rm -rf "$TEMP_HOME"' EXIT
mkdir -p "$TEMP_HOME/.claude" "$TEMP_HOME/.local/bin"
COACH_HEADROOM="$TEMP_HOME/coach-headroom"
printf '#!/bin/sh\nprintf "0 0\\n"\n' > "$COACH_HEADROOM"
chmod +x "$COACH_HEADROOM"

python3 - "$TEMP_HOME/.claude/provider-usage-snapshots.jsonl" <<'PY'
import json
import sys
from datetime import datetime, timezone

with open(sys.argv[1], 'w') as handle:
    json.dump({
        'schema': 'edge_agent_provider_usage_snapshot.v1',
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'providers': {
            'claude': {'windows': {'5h': {'left_pct': 25}}},
            'codex': {'windows': {'7d': {'left_pct': 80}}},
        },
    }, handle)
    handle.write('\n')
PY

output="$(HOME="$TEMP_HOME" EDGE_AGENT_COACH_HEADROOM="$COACH_HEADROOM" PATH="/usr/bin:/bin" bash "$ROOT/workflows/lib/usage-advisor.sh")"
[ "$output" = 'PREFER: codex (claude:25% codex:80%)' ]

echo "usage advisor snapshot fallback: ok"
