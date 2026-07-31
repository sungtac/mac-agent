import json
import os
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "bin"
WORKER = textwrap.dedent(
    """
    import json, os, sys, time
    from pathlib import Path
    sys.path.insert(0, os.environ["EDGE_AGENT_BIN"])
    import discord_bot_common as common
    common.REPO_LOCK_DIR = Path(os.environ["EDGE_AGENT_LOCK_DIR"])
    cwd = Path.cwd()
    output = cwd / os.environ["PILOT_OUTPUT"]
    try:
        with common.try_acquire_repo_lock(str(cwd)):
            output.write_text(os.environ["PILOT_VALUE"] + "\\n")
            time.sleep(float(os.environ.get("PILOT_HOLD_SECONDS", "0")))
            print(json.dumps({"status": "acquired"}))
    except common.RepoLockBusy:
        print(json.dumps({"status": "busy"}))
        raise SystemExit(75)
    """
)


class WorktreeParallelPilotTests(unittest.TestCase):
    def test_same_repository_worktrees_are_serialized_by_common_lock(self):
        with tempfile.TemporaryDirectory(prefix="edge-agent-worktree-pilot-") as temp:
            root = Path(temp) / "repo"
            first = Path(temp) / "first"
            second = Path(temp) / "second"
            lock_dir = Path(temp) / "locks"
            root.mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "README").write_text("pilot\n")
            subprocess.run(["git", "-C", str(root), "add", "README"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "pilot"],
                check=True,
            )
            subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", str(first)], check=True)
            subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "-b", "pilot-second", str(second)], check=True)

            env = {
                **os.environ,
                "PYTHONPATH": str(COMMON),
                "EDGE_AGENT_BIN": str(COMMON),
                "EDGE_AGENT_LOCK_DIR": str(lock_dir),
                "PILOT_OUTPUT": "result.txt",
                "PILOT_VALUE": "first",
                "PILOT_HOLD_SECONDS": "1",
            }
            holder = subprocess.Popen(
                ["python3", "-c", WORKER], cwd=first, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            time.sleep(0.2)
            contender_env = {**env, "PILOT_VALUE": "second", "PILOT_HOLD_SECONDS": "0"}
            contender = subprocess.run(
                ["python3", "-c", WORKER], cwd=second, env=contender_env,
                capture_output=True, text=True,
            )
            holder_stdout, holder_stderr = holder.communicate(timeout=5)

            self.assertEqual(holder.returncode, 0, holder_stderr)
            self.assertEqual(json.loads(holder_stdout)["status"], "acquired")
            self.assertEqual(contender.returncode, 75, contender.stderr)
            self.assertEqual(json.loads(contender.stdout)["status"], "busy")
            self.assertTrue((first / "result.txt").is_file())
            self.assertFalse((second / "result.txt").exists())


if __name__ == "__main__":
    unittest.main()
