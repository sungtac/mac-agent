#!/usr/bin/env python3
"""Inventory and optionally prune Codex health-repair worktrees.

Default mode is read-only. Pruning requires --apply and only targets direct
children of the configured repair root that are older than the retention
period and are not registered as active git worktrees.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


HOME = Path.home()
SOURCE_REPO = Path(os.environ.get("RODA_GEMMA_CODEX_SOURCE_REPO", "~/mac-agent")).expanduser().resolve()
REPAIR_ROOT = Path(os.environ.get("RODA_GEMMA_CODEX_REPAIR_ROOT", "~/.edge-agent-worktrees/health-repairs")).expanduser().resolve()
RETENTION_DAYS = int(os.environ.get("RODA_GEMMA_REPAIR_RETENTION_DAYS", "30"))
MAX_BYTES = int(os.environ.get("RODA_GEMMA_REPAIR_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))


def _registered_worktrees() -> set[Path]:
    result = subprocess.run(["/usr/bin/git", "-C", str(SOURCE_REPO), "worktree", "list", "--porcelain"], capture_output=True, text=True, check=False)
    paths: set[Path] = set()
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            paths.add(Path(line.removeprefix("worktree ")).resolve())
    return paths


def _size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _is_clean_worktree(path: Path) -> bool:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    # A failed status probe is ambiguous and must remain protected.
    return result.returncode == 0 and result.stdout == ""


def inventory(*, now: float | None = None) -> dict:
    current = now if now is not None else time.time()
    registered = _registered_worktrees()
    items: list[dict] = []
    if REPAIR_ROOT.is_dir():
        for path in sorted(REPAIR_ROOT.iterdir()):
            if not path.is_dir() or path.is_symlink():
                continue
            stat = path.stat()
            age_days = max(0.0, (current - stat.st_mtime) / 86400)
            size_bytes = _size(path)
            active = path.resolve() in registered
            clean = _is_clean_worktree(path) if not active else False
            eligible = age_days >= RETENTION_DAYS and not active and clean
            items.append({"path": str(path), "age_days": round(age_days, 2), "size_bytes": size_bytes, "active_worktree": active, "clean": clean, "eligible": eligible})
    total_bytes = sum(item["size_bytes"] for item in items)
    return {"repair_root": str(REPAIR_ROOT), "retention_days": RETENTION_DAYS, "max_bytes": MAX_BYTES, "total_bytes": total_bytes, "over_budget": total_bytes > MAX_BYTES, "items": items}


def prune(report: dict) -> list[str]:
    removed: list[str] = []
    registered = _registered_worktrees()
    for item in report["items"]:
        if not item["eligible"]:
            continue
        path = Path(item["path"]).resolve()
        if path.parent != REPAIR_ROOT or not path.is_dir():
            continue
        # Recheck the report at mutation time; inventory may be stale and a
        # repair worktree may have become active or dirty since then.
        if path in registered or not _is_clean_worktree(path):
            continue
        # Never force-remove a repair worktree.  Eligibility already requires
        # a clean status, and Git itself remains the final safety check.
        result = subprocess.run(["/usr/bin/git", "-C", str(SOURCE_REPO), "worktree", "remove", str(path)], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            removed.append(str(path))
    return removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--apply", action="store_true", help="remove only eligible, inactive worktrees")
    args = parser.parse_args()
    report = inventory()
    if args.apply:
        report["removed"] = prune(report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"repair_root={report['repair_root']} items={len(report['items'])} total_bytes={report['total_bytes']} over_budget={report['over_budget']}")
        for item in report["items"]:
            print(f"{item['path']} age_days={item['age_days']} active={item['active_worktree']} eligible={item['eligible']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
