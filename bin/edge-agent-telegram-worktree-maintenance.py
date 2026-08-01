#!/usr/bin/env python3
"""Inventory and safely prune Telegram task worktrees.

The default is read-only.  Apply mode removes only registered, clean,
terminal, unreferenced worktrees after the retention period.  Dirty or
ambiguous worktrees are always preserved for manual review.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from edge_agent_parallel_locks import repository_lifecycle_lock


SOURCE_REPO = Path(
    os.environ.get("TELEGRAM_AGENT_SOURCE_REPO", str(Path.home() / "mac-agent"))
).expanduser().resolve()
TASK_ROOT = Path(
    os.environ.get(
        "TELEGRAM_CODEX_TASK_WORKTREE_ROOT",
        str(Path.home() / ".edge-agent-worktrees" / "telegram-tasks"),
    )
).expanduser().resolve()
PLAN_ROOT = Path(
    os.environ.get("EDGE_AGENT_PLAN_DIR", str(Path.home() / ".edge-agent" / "plans"))
).expanduser().resolve()
SESSION_ROOT = Path(
    os.environ.get("EDGE_AGENT_SESSION_ROOT", str(Path.home() / ".edge-agent" / "sessions"))
).expanduser().resolve()
RETENTION_DAYS = max(1, int(os.environ.get("TELEGRAM_TASK_WORKTREE_RETENTION_DAYS", "7")))
TERMINAL_STATES = {"succeeded", "failed", "cancelled"}


def _registered_worktrees(source_repo: Path) -> set[Path]:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(source_repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        Path(line.removeprefix("worktree ")).resolve()
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    }


def _git_status(path: Path) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def _referenced_worktrees(plan_root: Path, session_root: Path) -> tuple[set[Path], set[Path]]:
    pending_plans: set[Path] = set()
    active_sessions: set[Path] = set()
    if plan_root.is_dir():
        for path in plan_root.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if payload.get("status") == "awaiting_approval" and payload.get("workspace"):
                pending_plans.add(Path(payload["workspace"]).expanduser().resolve())
    snapshots = session_root / "snapshots"
    if snapshots.is_dir():
        for path in snapshots.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if payload.get("status") in {"created", "running", "waiting", "handoff_ready"} and payload.get("worktree"):
                active_sessions.add(Path(payload["worktree"]).expanduser().resolve())
    return pending_plans, active_sessions


def _metadata(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads((path / ".edge-agent-task.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if payload.get("schema") != "edge_agent_worktree.v1":
        return None
    if payload.get("task_id") != path.name:
        return None
    return payload


def _age_days(payload: dict[str, Any], path: Path, now: float) -> float:
    stamp = payload.get("updated_at") or payload.get("created_at")
    try:
        created = datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError, OverflowError):
        created = path.stat().st_mtime
    return max(0.0, (now - created) / 86400)


def inventory(
    *,
    source_repo: Path = SOURCE_REPO,
    task_root: Path = TASK_ROOT,
    plan_root: Path = PLAN_ROOT,
    session_root: Path = SESSION_ROOT,
    retention_days: int = RETENTION_DAYS,
    now: float | None = None,
) -> dict[str, Any]:
    current = now if now is not None else time.time()
    registered = _registered_worktrees(source_repo)
    pending_plans, active_sessions = _referenced_worktrees(plan_root, session_root)
    items: list[dict[str, Any]] = []
    if task_root.is_dir():
        for path in sorted(task_root.iterdir()):
            if not path.is_dir() or path.is_symlink():
                continue
            payload = _metadata(path)
            status = payload.get("status") if payload else "invalid"
            dirty_state = _git_status(path)
            # A failed status probe is ambiguous and must remain protected;
            # only an explicit empty porcelain result proves cleanliness.
            dirty = dirty_state != ""
            registered_state = path.resolve() in registered
            pending = path.resolve() in pending_plans
            active = path.resolve() in active_sessions
            age_days = _age_days(payload, path, current) if payload else 0.0
            eligible = bool(
                payload
                and registered_state
                and status in TERMINAL_STATES
                and not dirty
                and not pending
                and not active
                and age_days >= retention_days
            )
            items.append(
                {
                    "path": str(path),
                    "task_id": payload.get("task_id") if payload else None,
                    "role": payload.get("role") if payload else None,
                    "status": status,
                    "age_days": round(age_days, 2),
                    "registered_worktree": registered_state,
                    "dirty": dirty,
                    "pending_approval": pending,
                    "active_session": active,
                    "eligible": eligible,
                }
            )
    return {
        "schema": "edge_agent.telegram_worktree_inventory.v1",
        "source_repo": str(source_repo),
        "task_root": str(task_root),
        "retention_days": retention_days,
        "items": items,
        "eligible_count": sum(1 for item in items if item["eligible"]),
    }


def prune(report: dict[str, Any], *, source_repo: Path = SOURCE_REPO, task_root: Path = TASK_ROOT) -> list[str]:
    removed: list[str] = []
    for item in report.get("items", []):
        if not item.get("eligible"):
            continue
        path = Path(item["path"]).resolve()
        if path.parent != task_root or not path.is_dir():
            continue
        with repository_lifecycle_lock(source_repo):
            result = subprocess.run(
                ["/usr/bin/git", "-C", str(source_repo), "worktree", "remove", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
        if result.returncode == 0:
            removed.append(str(path))
    return removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = inventory()
    if args.apply:
        report["removed"] = prune(report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"task_root={report['task_root']} items={len(report['items'])} "
            f"eligible={report['eligible_count']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
