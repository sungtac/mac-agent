import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "edge_agent_health_maintenance",
    ROOT / "bin" / "edge-agent-health-maintenance.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class HealthMaintenanceTests(unittest.TestCase):
    def make_repo(self, path: Path) -> None:
        path.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        (path / "tracked.txt").write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True)
        subprocess.run(
            ["git", "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-qm", "initial"],
            cwd=path,
            check=True,
        )

    def test_dirty_worktree_is_not_clean(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repair"
            self.make_repo(repo)
            (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            self.assertFalse(MODULE._is_clean_worktree(repo))

    def test_clean_worktree_is_clean(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repair"
            self.make_repo(repo)
            self.assertTrue(MODULE._is_clean_worktree(repo))

    def test_prune_never_uses_force(self):
        report = {"items": [{"path": "/tmp/repair", "eligible": True}]}
        with patch.object(MODULE, "REPAIR_ROOT", Path("/tmp").resolve()), patch.object(MODULE, "SOURCE_REPO", Path("/repo")), \
                patch.object(MODULE, "_registered_worktrees", return_value=set()), \
                patch.object(MODULE, "_is_clean_worktree", return_value=True):
            with patch.object(MODULE.subprocess, "run", return_value=type("Result", (), {"returncode": 0})()) as run:
                with patch("pathlib.Path.is_dir", return_value=True):
                    MODULE.prune(report)
        command = run.call_args.args[0]
        self.assertNotIn("--force", command)


if __name__ == "__main__":
    unittest.main()
