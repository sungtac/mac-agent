"""Read-only audit for parallel worktree manifests and git worktrees.

The audit never removes, resets, or repairs a worktree. It reports missing
manifests, missing worktrees, path mismatches, and unregistered worktrees so a
human or a separately approved recovery step can decide what to do.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from edge_agent_parallel_worktree import WorktreeManager
from edge_agent_parallel_locks import FileReservation, reservation_age_seconds, reservation_is_stale


@dataclass(frozen=True)
class AuditFinding:
    code: str
    severity: str
    task_id: str = ""
    path: str = ""
    detail: str = ""


def _git_worktrees(repo_root: Path) -> set[Path]:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "git worktree list failed").strip())
    paths: set[Path] = set()
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            paths.add(Path(line.removeprefix("worktree ")).resolve())
    return paths


def audit(
    manager: WorktreeManager,
    repo_root: str | Path,
    *,
    reservation_ttl_seconds: float = 3600,
) -> list[AuditFinding]:
    repo = Path(repo_root).expanduser().resolve()
    git_paths = _git_worktrees(repo)
    findings: list[AuditFinding] = []
    registered: set[Path] = set()

    for manifest_path in sorted(manager.manifest_root.glob("*.json")):
        try:
            manifest = manager.read(manifest_path.stem)
        except Exception as exc:
            findings.append(AuditFinding("invalid_manifest", "high", path=str(manifest_path), detail=str(exc)))
            continue
        worktree = Path(manifest.worktree_path).resolve()
        registered.add(worktree)
        if not worktree.exists():
            findings.append(AuditFinding("missing_worktree", "high", manifest.task_id, str(worktree), "manifest points to a missing path"))
        elif worktree not in git_paths:
            findings.append(AuditFinding("unregistered_git_worktree", "high", manifest.task_id, str(worktree), "path exists but git does not list it as a worktree"))
        if Path(manifest.repo_root).resolve() != repo:
            findings.append(AuditFinding("repo_mismatch", "high", manifest.task_id, str(worktree), f"manifest repo is {manifest.repo_root}"))

    managed_root = manager.worktree_root.resolve()
    for path in sorted(git_paths):
        if path == repo or managed_root not in path.parents:
            continue
        if path not in registered:
            findings.append(AuditFinding("orphan_worktree", "medium", path=str(path), detail="git worktree has no parallel manifest"))

    try:
        reservation_store = FileReservation(repo, state_root=manager.state_root)
        reservations = reservation_store.active()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        findings.append(AuditFinding("invalid_reservation_registry", "high", detail=str(exc)))
    else:
        reservation_registry = str(reservation_store.registry)
        for record in reservations:
            age = reservation_age_seconds(record)
            if reservation_is_stale(record, ttl_seconds=reservation_ttl_seconds):
                age_text = "unknown" if age is None else f"{age:.0f}s"
                findings.append(
                    AuditFinding(
                        "stale_reservation",
                        "medium",
                        task_id=str(record.get("task_id", "")),
                        path=reservation_registry,
                        detail=f"active reservation heartbeat age={age_text}; human review required",
                    )
                )
    return findings


def audit_payload(manager: WorktreeManager, repo_root: str | Path, *, reservation_ttl_seconds: float = 3600) -> dict:
    findings = audit(manager, repo_root, reservation_ttl_seconds=reservation_ttl_seconds)
    return {
        "schema": "edge_agent_parallel_audit.v1",
        "repo_root": str(Path(repo_root).expanduser().resolve()),
        "manifest_root": str(manager.manifest_root),
        "worktree_root": str(manager.worktree_root),
        "reservation_ttl_seconds": reservation_ttl_seconds,
        "finding_count": len(findings),
        "findings": [asdict(item) for item in findings],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="audit parallel worktrees without changing them")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state-root")
    parser.add_argument("--worktree-root")
    parser.add_argument(
        "--reservation-ttl-seconds",
        type=float,
        default=float(os.environ.get("EDGE_AGENT_RESERVATION_TTL_SECONDS", "3600")),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    manager = WorktreeManager(state_root=args.state_root, worktree_root=args.worktree_root)
    payload = audit_payload(manager, args.repo, reservation_ttl_seconds=args.reservation_ttl_seconds)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["finding_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
