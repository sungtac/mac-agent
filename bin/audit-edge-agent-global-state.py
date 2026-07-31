#!/usr/bin/env python3
"""Read-only inventory of Edge Agent global state and lock paths."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


HOME = Path.home()


def _entry(path: Path, owner: str, kind: str, source: str, notes: str = "") -> dict:
    path = path.expanduser()
    exists = path.exists()
    item = {
        "path": str(path),
        "owner": owner,
        "kind": kind,
        "source": source,
        "exists": exists,
        "is_dir": path.is_dir() if exists else False,
    }
    if exists:
        stat = path.stat()
        item["size_bytes"] = stat.st_size if path.is_file() else None
        item["modified_at"] = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat()
    if notes:
        item["notes"] = notes
    return item


def collect() -> dict:
    claude = HOME / ".claude"
    return {
        "schema": "edge_agent_global_state_inventory.v1",
        "read_only": True,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "items": [
            _entry(
                claude / "discord-bot" / "repo-locks",
                "Telegram + Discord bots",
                "lock_directory",
                "bin/telegram-agent-bot.py; bin/discord_bot_common.py",
                "worktree adoption requires an original-repository lock key",
            ),
            _entry(
                claude / "hooks-state" / "telegram-bridge-locks",
                "Telegram bots",
                "singleton_lock_directory",
                "bin/telegram-agent-bot.py",
            ),
            _entry(
                claude / "hooks-state" / "work-log",
                "work-log Stop hook + Discord retry",
                "marker_and_log_directory",
                "hooks/work-log-stop-check.sh; bin/discord-bot.py",
            ),
            _entry(
                claude / "hooks-state" / "verify-task-nag",
                "verify Stop hook",
                "session_marker_directory",
                "hooks/verify-task-stop-check.sh",
            ),
            _entry(
                claude / "hooks-state" / "session-cost-gate-nag",
                "session cost Stop hook",
                "session_marker_directory",
                "hooks/session-cost-gate-stop-check.sh",
            ),
            _entry(
                claude / "hooks-state" / "usage-routing-nag",
                "usage routing hook",
                "session_marker_directory",
                "hooks/usage-routing-check.sh",
            ),
            _entry(
                claude / "discord-bot" / "pending",
                "Discord bot + retry workflows",
                "pending_job_directory",
                "bin/discord-bot.py; workflows/verify-task-v2.js",
                "deleting entries can break reply-triggered retries",
            ),
            _entry(
                claude / "nano-gate-events.jsonl",
                "nano event store",
                "event_ledger",
                "workflows/lib/nano-event-store.js",
                "missing is expected until a production nano run records an event",
            ),
            _entry(
                claude / "verify-task-v2-history.jsonl",
                "verify-task-v2 workflow",
                "history_ledger",
                "workflows/verify-task-v2.js",
            ),
            _entry(
                HOME / ".claude-watchdog",
                "watchdog + claude-main",
                "watchdog_state_directory",
                "external to mac-agent",
                "not covered by provider CLI sandbox",
            ),
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()
    report = collect()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print("Edge Agent global state inventory (read-only)")
    for item in report["items"]:
        state = "present" if item["exists"] else "missing"
        print(f"- {state}: {item['path']} [{item['kind']}; owner={item['owner']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
