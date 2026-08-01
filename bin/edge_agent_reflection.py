#!/usr/bin/env python3
"""Durable task lifecycle and reflection evidence; never edits agent rules."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("EDGE_AGENT_EVIDENCE_DIR", str(Path.home() / ".edge-agent"))).expanduser()
REFLECTION_DIR = ROOT / "reflections"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        worktree / ".edge-agent-task.json",
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
    path = worktree / ".edge-agent-task.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"worktree metadata is unreadable: {path}") from exc
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
