#!/usr/bin/env python3
"""Read the latest local provider-usage snapshot with an age classification."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path


def read_latest(path: Path, max_age_seconds: int) -> dict:
    path = path.expanduser()
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    latest = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("schema") == "edge_agent_provider_usage_snapshot.v1":
            latest = candidate
    if latest is None:
        return {"status": "invalid", "path": str(path)}
    try:
        observed = datetime.fromisoformat(latest["observed_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError):
        return {"status": "invalid", "path": str(path)}
    age_seconds = max(0, int(time.time() - observed))
    status = "fresh" if age_seconds <= max_age_seconds else "stale"
    return {"status": status, "age_seconds": age_seconds, "path": str(path), "snapshot": latest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(Path.home() / ".claude" / "provider-usage-snapshots.jsonl"))
    parser.add_argument("--max-age-seconds", type=int, default=3600)
    args = parser.parse_args()
    if args.max_age_seconds < 0:
        parser.error("--max-age-seconds must be non-negative")
    print(json.dumps(read_latest(Path(args.input), args.max_age_seconds), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
