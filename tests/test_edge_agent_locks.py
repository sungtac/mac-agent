import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "edge_agent_locks", ROOT / "bin" / "edge_agent_locks.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class EdgeAgentLockTests(unittest.TestCase):
    def test_non_git_path_falls_back_to_resolved_path(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "child"
            path.mkdir()
            self.assertEqual(MODULE.canonical_repository_root(path), path.resolve())

    def test_current_repository_resolves_to_repository_root(self):
        common = Path(
            subprocess.check_output(
                ["git", "-C", str(ROOT / "tests"), "rev-parse", "--git-common-dir"],
                text=True,
            ).strip()
        )
        if not common.is_absolute():
            common = (ROOT / "tests") / common
        expected = common.resolve().parent
        self.assertEqual(MODULE.canonical_repository_root(ROOT / "tests"), expected)


if __name__ == "__main__":
    unittest.main()
