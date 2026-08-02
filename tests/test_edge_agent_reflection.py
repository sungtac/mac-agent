import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import edge_agent_reflection as reflection  # noqa: E402


class WorktreeMetadataTests(unittest.TestCase):
    def test_new_metadata_is_outside_worktree_and_legacy_is_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "task-1"
            worktree.mkdir()
            metadata_root = root / "metadata"
            with patch.object(reflection, "WORKTREE_METADATA_ROOT", metadata_root):
                reflection.write_worktree_metadata(worktree, task_id="task-1", role="codex")
                self.assertFalse((worktree / ".edge-agent-task.json").exists())
                payload, path = reflection.read_worktree_metadata(worktree)
                self.assertEqual(payload["task_id"], "task-1")
                self.assertEqual(path, metadata_root / "task-1.json")
                reflection.update_worktree_metadata(worktree, task_id="task-1", role="codex", status="succeeded")
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "succeeded")

            legacy = root / "legacy"
            legacy.mkdir()
            legacy_path = legacy / ".edge-agent-task.json"
            legacy_path.write_text(json.dumps({"schema": "edge_agent_worktree.v1", "task_id": "legacy", "role": "claude"}), encoding="utf-8")
            with patch.object(reflection, "WORKTREE_METADATA_ROOT", root / "missing-metadata"):
                payload, path = reflection.read_worktree_metadata(legacy)
            self.assertEqual(payload["role"], "claude")
            self.assertEqual(path, legacy_path)


if __name__ == "__main__":
    unittest.main()
