#!/usr/bin/env python3
"""Inventory and safely prune Telegram task worktrees.

The default is read-only.  Apply mode removes only registered, clean,
terminal, unreferenced worktrees after the retention period.  Dirty worktrees
require the explicit ``--archive-dirty`` option; they are archived outside
the worktree before a forced reclaim.  Dirty or ambiguous worktrees are
otherwise preserved for manual review.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from edge_agent_parallel_locks import repository_lifecycle_lock
from edge_agent_reflection import read_worktree_metadata


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
ARCHIVE_ROOT = Path(
    os.environ.get("EDGE_AGENT_WORKTREE_ARCHIVE_ROOT", str(Path.home() / ".edge-agent" / "worktree-archives"))
).expanduser().resolve()


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
        payload, _ = read_worktree_metadata(path)
    except (OSError, UnicodeError, RuntimeError, json.JSONDecodeError):
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
                    "dirty_status": dirty_state,
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


def _archive_worktree(path: Path, item: dict[str, Any], *, archive_root: Path) -> Path:
    """Create a recoverable audit archive before force-reclaiming a dirty tree."""
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError("worktree archive target is not a real directory")
    archive_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(archive_root, 0o700)
    task_id = path.name
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    archive = archive_root / f"{task_id}-{digest}-{int(time.time())}.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(path, arcname=path.name, recursive=True, filter=lambda info: None if ".git" in Path(info.name).parts else info)
        manifest = json.dumps(
            {
                "schema": "edge_agent.worktree_archive.v1",
                "source_path": str(path),
                "task_id": task_id,
                "status": item.get("status"),
                "dirty": item.get("dirty"),
                "archived_epoch": time.time(),
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        info = tarfile.TarInfo(f"{path.name}/ARCHIVE-MANIFEST.json")
        info.size = len(manifest)
        info.mode = 0o600
        stream.addfile(info, fileobj=io.BytesIO(manifest))
    os.chmod(archive, 0o600)
    return archive


def prune(
    report: dict[str, Any],
    *,
    source_repo: Path = SOURCE_REPO,
    task_root: Path = TASK_ROOT,
    archive_dirty: bool = False,
    archive_root: Path = ARCHIVE_ROOT,
) -> list[str]:
    removed: list[str] = []
    for item in report.get("items", []):
        eligible = bool(item.get("eligible"))
        dirty_archive_candidate = bool(
            archive_dirty
            and item.get("dirty")
            and item.get("dirty_status") != "unavailable"
            and item.get("status") in TERMINAL_STATES
            and item.get("registered_worktree")
            and not item.get("pending_approval")
            and not item.get("active_session")
            and float(item.get("age_days") or 0.0) >= float(report.get("retention_days") or RETENTION_DAYS)
        )
        if not eligible and not dirty_archive_candidate:
            continue
        path = Path(item["path"]).resolve()
        if path.parent != task_root or not path.is_dir():
            continue
        if dirty_archive_candidate:
            try:
                archive = _archive_worktree(path, item, archive_root=archive_root)
            except (OSError, RuntimeError, tarfile.TarError) as exc:
                item["reclaim_error"] = type(exc).__name__
                continue
        with repository_lifecycle_lock(source_repo):
            command = "remove"
            args = ["/usr/bin/git", "-C", str(source_repo), "worktree", command]
            if dirty_archive_candidate:
                args.append("--force")
            args.append(str(path))
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False,
            )
        if result.returncode == 0:
            removed.append(str(path))
            if dirty_archive_candidate:
                item["archive"] = str(archive)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--archive-dirty",
        action="store_true",
        help="archive old unreferenced terminal dirty worktrees before force removal",
    )
    args = parser.parse_args()
    report = inventory()
    if args.apply:
        report["removed"] = prune(report, archive_dirty=args.archive_dirty)
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
