#!/usr/bin/env python3
"""Durable task lifecycle and reflection evidence; never edits agent rules."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from edge_agent_secure_paths import ensure_private_directory, read_text, reject_symlink_components


ROOT = Path(os.environ.get("EDGE_AGENT_EVIDENCE_DIR", str(Path.home() / ".edge-agent"))).expanduser()
REFLECTION_DIR = ROOT / "reflections"
WORKTREE_METADATA_ROOT = Path(
    os.environ.get("EDGE_AGENT_WORKTREE_METADATA_ROOT", str(ROOT / "worktree-metadata"))
).expanduser()


def worktree_metadata_path(worktree: Path, *, task_id: str | None = None) -> Path:
    """Return metadata outside the Git worktree to keep runtime state clean."""
    identifier = str(task_id or worktree.name).strip()
    if not identifier or any(char in identifier for char in "/\\\x00"):
        raise ValueError("unsafe worktree metadata identifier")
    return WORKTREE_METADATA_ROOT / f"{identifier}.json"


def read_worktree_metadata(worktree: Path) -> tuple[dict[str, Any], Path]:
    """Read new external metadata, falling back to legacy in-tree state."""
    candidates = [worktree_metadata_path(worktree), worktree / ".edge-agent-task.json"]
    for path in candidates:
        try:
            payload = json.loads(read_text(path))
        except (OSError, UnicodeError, RuntimeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload, path
    raise RuntimeError(f"worktree metadata is unreadable: {worktree}")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    ensure_private_directory(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=".evidence-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def write_worktree_metadata(worktree: Path, *, task_id: str, role: str, status: str = "active") -> None:
    now = datetime.now(timezone.utc).isoformat()
    _atomic_json(
        worktree_metadata_path(worktree, task_id=task_id),
        {
            "schema": "edge_agent_worktree.v1",
            "task_id": task_id,
            "role": role,
            "status": status,
            "created_at": now,
            "updated_at": now,
            "worktree": str(worktree),
        },
    )


def update_worktree_metadata(worktree: Path, *, task_id: str, role: str, status: str) -> None:
    """Update lifecycle state without replacing creation ownership evidence."""
    try:
        payload, path = read_worktree_metadata(worktree)
    except RuntimeError as exc:
        raise RuntimeError(f"worktree metadata is unreadable: {worktree}") from exc
    if (
        payload.get("schema") != "edge_agent_worktree.v1"
        or payload.get("task_id") != task_id
        or payload.get("role") != role
    ):
        raise RuntimeError(f"worktree metadata ownership mismatch: {path}")
    payload["status"] = status
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    if status in {"succeeded", "failed", "cancelled"}:
        payload["completed_at"] = payload["updated_at"]
    _atomic_json(path, payload)


def write_reflection(*, task_id: str, role: str, workspace: str, status: str, response_preview: str = "", error: str = "") -> None:
    _atomic_json(
        REFLECTION_DIR / f"{task_id}.json",
        {
            "schema": "edge_agent_reflection.v1",
            "task_id": task_id,
            "role": role,
            "workspace": workspace,
            "status": status,
            "what_changed": "",
            "what_was_learned": "",
            "what_remains_risky": error,
            "verification_evidence": "",
            "response_preview": response_preview[:1000],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "rule_change_required": False,
        },
    )
