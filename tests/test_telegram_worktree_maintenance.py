#!/usr/bin/env python3
"""Tests for safe Telegram worktree inventory and pruning eligibility."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "bin"))
SPEC = importlib.util.spec_from_file_location(
    "telegram_worktree_maintenance",
    ROOT / "bin" / "edge-agent-telegram-worktree-maintenance.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class TelegramWorktreeMaintenanceTests(unittest.TestCase):
    def make_tree(self, root: Path, name: str, status: str = "succeeded") -> Path:
        path = root / name
        path.mkdir()
        (path / ".edge-agent-task.json").write_text(json.dumps({
            "schema": "edge_agent_worktree.v1",
            "task_id": name,
            "role": "codex",
            "status": status,
            "created_at": "2026-07-01T00:00:00+00:00",
            "updated_at": "2026-07-01T00:00:00+00:00",
        }), encoding="utf-8")
        return path

    def test_only_clean_terminal_unreferenced_old_worktrees_are_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            eligible = self.make_tree(root, "task-eligible")
            active = self.make_tree(root, "task-active", status="active")
            dirty = self.make_tree(root, "task-dirty")
            pending = self.make_tree(root, "task-pending")
            with patch.object(MODULE, "_registered_worktrees", return_value={eligible.resolve(), active.resolve(), dirty.resolve(), pending.resolve()}), \
                    patch.object(MODULE, "_git_status", side_effect=lambda path: " M file.py" if path == dirty else ""), \
                    patch.object(MODULE, "_referenced_worktrees", return_value=({pending.resolve()}, {active.resolve()})):
                report = MODULE.inventory(
                    source_repo=root,
                    task_root=root,
                    plan_root=root / "plans",
                    session_root=root / "sessions",
                    retention_days=7,
                    now=1785567524,
                )
            by_name = {Path(item["path"]).name: item for item in report["items"]}
            self.assertTrue(by_name["task-eligible"]["eligible"])
            self.assertFalse(by_name["task-active"]["eligible"])
            self.assertFalse(by_name["task-dirty"]["eligible"])
            self.assertFalse(by_name["task-pending"]["eligible"])

    def test_invalid_or_unregistered_worktree_is_never_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "invalid"
            invalid.mkdir()
            with patch.object(MODULE, "_registered_worktrees", return_value=set()), \
                    patch.object(MODULE, "_git_status", return_value=""):
                report = MODULE.inventory(
                    source_repo=root,
                    task_root=root,
                    plan_root=root / "plans",
                    session_root=root / "sessions",
                    retention_days=1,
                    now=1785567524,
                )
            self.assertEqual(report["items"][0]["status"], "invalid")
            self.assertFalse(report["items"][0]["eligible"])

    def test_status_probe_failure_is_preserved_as_dirty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self.make_tree(root, "task-status-error")
            with patch.object(MODULE, "_registered_worktrees", return_value={candidate.resolve()}), \
                    patch.object(MODULE, "_git_status", return_value="unavailable"), \
                    patch.object(MODULE, "_referenced_worktrees", return_value=(set(), set())):
                report = MODULE.inventory(
                    source_repo=root,
                    task_root=root,
                    plan_root=root / "plans",
                    session_root=root / "sessions",
                    retention_days=1,
                    now=1785567524,
                )
            self.assertTrue(report["items"][0]["dirty"])
            self.assertFalse(report["items"][0]["eligible"])


if __name__ == "__main__":
    unittest.main()
