#!/usr/bin/env python3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "edge-agent-provider-sandbox.sh"
PROTECTED_TARGET = Path("/Users/edge_ai/.openclaw/workspace/team_os/.edge-agent-canary-probe")


class ProviderSandboxCanaryTests(unittest.TestCase):
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

    def test_codex_legacy_shared_workspace_is_rejected_before_nested_sandbox(self):
        with tempfile.TemporaryDirectory(prefix="edge-agent-codex-wrapper-") as temp_dir:
            fake_codex = Path(temp_dir) / "codex"
            fake_codex.write_text("#!/bin/sh\nexit 0\n")
            fake_codex.chmod(0o755)
            result = subprocess.run(
                [str(WRAPPER), str(fake_codex), "-C", "/Users/edge_ai/.openclaw/workspace"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)

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
