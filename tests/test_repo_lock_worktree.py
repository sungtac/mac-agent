import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
SPEC = importlib.util.spec_from_file_location(
    "discord_bot_common", ROOT / "bin" / "discord_bot_common.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class RepoLockWorktreeTests(unittest.TestCase):
    def test_checkout_and_worktree_share_canonical_lock_path(self):
        with tempfile.TemporaryDirectory() as temp:
            MODULE.REPO_LOCK_DIR = Path(temp) / "locks"
            root = Path(temp) / "repo"
            worktree = Path(temp) / "worktree"
            root.mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "file.txt").write_text("initial\n")
            subprocess.run(["git", "-C", str(root), "add", "file.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"],
                check=True,
            )
            subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", str(worktree)], check=True)
            self.assertEqual(
                MODULE._repo_lock_path(str(root)),
                MODULE._repo_lock_path(str(worktree)),
            )

    def test_lock_is_released_after_context_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            MODULE.REPO_LOCK_DIR = Path(temp) / "locks"
            path = Path(temp) / "not-a-repo"
            path.mkdir()
            with MODULE.try_acquire_repo_lock(str(path)):
                with self.assertRaises(MODULE.RepoLockBusy):
                    with MODULE.try_acquire_repo_lock(str(path)):
                        pass
            with MODULE.try_acquire_repo_lock(str(path)):
                pass


if __name__ == "__main__":
    unittest.main()
