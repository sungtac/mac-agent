#!/usr/bin/env python3
import fcntl
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "edge-agent-provider-sandbox.sh"
PROTECTED_ROOT = Path.home() / ".edge-agent" / "protected-canary"
TEST_LOCK_ROOT = Path(os.environ.get("EDGE_AGENT_TEST_LOCK_ROOT", str(PROTECTED_ROOT.parent))).expanduser()
PROTECTED_TARGET = PROTECTED_ROOT / ".edge-agent-canary-probe"


class ProviderSandboxCanaryTests(unittest.TestCase):
    def setUp(self):
        lock_path = TEST_LOCK_ROOT / "protected-canary.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fixture_lock = lock_path.open("a+")
        fcntl.flock(self._fixture_lock.fileno(), fcntl.LOCK_EX)
        PROTECTED_ROOT.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        try:
            PROTECTED_ROOT.rmdir()
        except OSError:
            pass
        fcntl.flock(self._fixture_lock.fileno(), fcntl.LOCK_UN)
        self._fixture_lock.close()

    def _make_worktree(self, temp_dir: str) -> Path:
        root = Path(temp_dir) / "repo"
        worktree = Path(temp_dir) / "worktree"
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "README").write_text("canary\n")
        subprocess.run(["git", "-C", str(root), "add", "README"], check=True)
        subprocess.run(
            [
                "git", "-C", str(root), "-c", "user.name=Test",
                "-c", "user.email=test@example.invalid", "commit", "-qm", "canary",
            ],
            check=True,
        )
        subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", str(worktree)], check=True)
        return worktree

    def test_child_process_cannot_write_protected_root(self):
        self.assertFalse(PROTECTED_TARGET.exists(), "protected canary target unexpectedly exists")
        result = subprocess.run(
            [str(WRAPPER), "/bin/sh", "-c", f"touch '{PROTECTED_TARGET}'"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(PROTECTED_TARGET.exists())
        self.assertIn("Operation not permitted", result.stderr)

    def test_child_process_can_write_declared_non_protected_temp_path(self):
        with tempfile.TemporaryDirectory(prefix="edge-agent-provider-canary-") as temp_dir:
            target = Path(temp_dir) / "allowed"
            result = subprocess.run(
                [str(WRAPPER), "/bin/sh", "-c", f"touch '{target}'"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(target.is_file())

    def test_wrapper_preserves_provider_arguments(self):
        result = subprocess.run(
            [str(WRAPPER), "/bin/echo", "provider-arg", "--", "literal"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "provider-arg -- literal")

    def test_worktree_cwd_keeps_protected_root_blocked(self):
        with tempfile.TemporaryDirectory(prefix="edge-agent-worktree-canary-") as temp_dir:
            worktree = self._make_worktree(temp_dir)
            allowed_target = worktree / "provider-output"
            blocked_result = subprocess.run(
                [str(WRAPPER), "/bin/sh", "-c", f"touch '{PROTECTED_TARGET}'"],
                cwd=worktree,
                capture_output=True,
                text=True,
                check=False,
            )
            allowed_result = subprocess.run(
                [str(WRAPPER), "/bin/sh", "-c", f"touch '{allowed_target}'"],
                cwd=worktree,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(blocked_result.returncode, 0)
            self.assertFalse(PROTECTED_TARGET.exists())
            self.assertEqual(allowed_result.returncode, 0, allowed_result.stderr)
            self.assertTrue(allowed_target.is_file())

    def test_codex_retired_openclaw_workspace_is_rejected_before_nested_sandbox(self):
        with tempfile.TemporaryDirectory(prefix="edge-agent-codex-wrapper-") as temp_dir:
            fake_codex = Path(temp_dir) / "codex"
            fake_codex.write_text("#!/bin/sh\nexit 0\n")
            fake_codex.chmod(0o755)
            result = subprocess.run(
                [str(WRAPPER), str(fake_codex), "-C", str(Path.home() / ".openclaw" / "workspace")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_codex_combined_cd_option_is_checked(self):
        with tempfile.TemporaryDirectory(prefix="edge-agent-codex-wrapper-") as temp_dir:
            fake_codex = Path(temp_dir) / "codex"
            fake_codex.write_text("#!/bin/sh\nexit 0\n")
            fake_codex.chmod(0o755)
            result = subprocess.run(
                [str(WRAPPER), str(fake_codex), "-C" + str(Path.home() / ".openclaw" / "workspace")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_codex_relative_traversal_to_retired_path_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="edge-agent-codex-wrapper-") as temp_dir:
            fake_codex = Path(temp_dir) / "codex"
            fake_codex.write_text("#!/bin/sh\nexit 0\n")
            fake_codex.chmod(0o755)
            retired = Path.home() / ".openclaw" / "nonexistent"
            relative_retired = os.path.relpath(retired, ROOT)
            result = subprocess.run(
                [str(WRAPPER), str(fake_codex), "-C", relative_retired],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_codex_options_after_separator_are_not_reinterpreted(self):
        with tempfile.TemporaryDirectory(prefix="edge-agent-codex-wrapper-") as temp_dir:
            fake_codex = Path(temp_dir) / "codex"
            fake_codex.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
            fake_codex.chmod(0o755)
            result = subprocess.run(
                [str(WRAPPER), str(fake_codex), "--", "-C", str(Path.home() / ".openclaw" / "workspace")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("-C", result.stdout)

    def test_review_mode_denies_repository_writes(self):
        with tempfile.TemporaryDirectory(prefix="edge-agent-review-canary-") as temp_dir:
            target = Path(temp_dir) / "review-write"
            result = subprocess.run(
                [str(WRAPPER), "/bin/sh", "-c", f"touch '{target}'"],
                cwd=temp_dir,
                env={**__import__("os").environ, "EDGE_AGENT_PROVIDER_MODE": "review"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())



if __name__ == "__main__":
    unittest.main()
