import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "verify-task-harness.py"
SPEC = importlib.util.spec_from_file_location("verify_task_harness", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class VerifyTaskHarnessTests(unittest.TestCase):
    def test_light_policy_is_deterministic_and_allows_code_paths(self):
        policy = MODULE.classify("작은 버그 수정", ["src/example.ts"])
        self.assertEqual(policy["track"], "light")

    def test_sensitive_and_dependency_paths_force_full(self):
        policy = MODULE.classify("의존성 업데이트", ["package.json"])
        self.assertEqual(policy["track"], "full")
        self.assertIn("new_dependency", policy["reasons"])

    def test_init_and_snapshot_write_file_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "example.ts").write_text("export const value = 1;\n", encoding="utf-8")
            subprocess.run(["git", "add", "example.ts"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-qm", "initial"], cwd=repo, check=True)
            (repo / "example.ts").write_text("export const value = 2;\n", encoding="utf-8")
            run_dir = repo / ".verify" / "runs" / "TASK-001"
            init = MODULE.init_run(repo, "example.ts 수정", run_dir, False)
            snap = MODULE.snapshot(repo, "example.ts 수정", run_dir, False)
            self.assertEqual(init["policy"]["track"], "light")
            self.assertEqual(snap["files_changed"], ["example.ts"])
            self.assertTrue((run_dir / "current.diff").is_file())
            self.assertTrue((run_dir / "task.json").is_file())


if __name__ == "__main__":
    unittest.main()
