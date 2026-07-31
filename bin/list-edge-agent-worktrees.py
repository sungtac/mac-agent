#!/usr/bin/env python3
"""List Edge Agent worktrees and lifecycle metadata; never deletes anything."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path.home() / ".edge-agent-worktrees"


def main() -> int:
    rows = []
    for path in sorted(ROOT.glob("**/.edge-agent-task.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append({"worktree": str(path.parent), **data})
    print(json.dumps({"schema": "edge_agent_worktrees.v1", "count": len(rows), "items": rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
