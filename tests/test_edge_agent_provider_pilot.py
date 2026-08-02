from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "edge_agent_provider_pilot.py"


def load_module():
    spec = importlib.util.spec_from_file_location("edge_agent_provider_pilot_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class ProviderPilotTests(unittest.TestCase):
    def _clean_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "README.md").write_text("pilot\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "pilot"], check=True)
        return repo

    def test_plan_requires_clean_worktree_and_nonempty_prompt(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._clean_repo(root)
            prompt = root / "prompt.txt"
            prompt.write_text("read-only canary\n", encoding="utf-8")
            with patch.object(module, "_capability", return_value=(True, "codex_provider: available")), patch.object(module, "_usage_gate", return_value=(True, "PROCEED codex 7d창 잔여 90%")):
                plan = module.build_plan("codex", prompt, repo)
            self.assertTrue(all(item["ok"] for item in plan["checks"]), plan)
            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            with patch.object(module, "_capability", return_value=(True, "codex_provider: available")), patch.object(module, "_usage_gate", return_value=(True, "PROCEED codex 7d창 잔여 90%")):
                dirty_plan = module.build_plan("codex", prompt, repo)
            worktree = next(item for item in dirty_plan["checks"] if item["name"] == "worktree")
            self.assertFalse(worktree["ok"])

            oversized = root / "oversized-prompt.txt"
            oversized.write_bytes(b"x" * (module.MAX_PROMPT_BYTES + 1))
            with patch.object(module, "_capability", return_value=(True, "codex_provider: available")), patch.object(module, "_usage_gate", return_value=(True, "PROCEED codex 7d창 잔여 90%")):
                oversized_plan = module.build_plan("codex", oversized, repo)
            prompt_check = next(item for item in oversized_plan["checks"] if item["name"] == "prompt")
            self.assertFalse(prompt_check["ok"])
            self.assertIn("exceeds", prompt_check["detail"])

    def test_execution_requires_explicit_confirmation_without_spawning(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._clean_repo(root)
            prompt = root / "prompt.txt"
            prompt.write_text("read-only canary\n", encoding="utf-8")
            result = subprocess.run(
                ["python3", str(SCRIPT), "--provider", "codex", "--prompt-file", str(prompt), "--workdir", str(repo), "--execute", "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertIn("confirm-live-provider", payload["errors"][0])

    def test_execution_timeout_has_a_bounded_upper_limit(self):
        result = subprocess.run(
            [
                "python3", str(SCRIPT), "--provider", "codex", "--prompt-file", "/tmp/missing-prompt",
                "--workdir", "/tmp", "--timeout", str(load_module().MAX_TIMEOUT_SECONDS + 1), "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be <=", result.stderr)

    def test_blocked_plan_creates_a_durable_improvement_task(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(module.os.environ, {"EDGE_AGENT_IMPROVEMENT_ROOT": td}, clear=False):
                plan = module.attach_improvement_task({
                    "ok": False,
                    "provider": "claude",
                    "checks": [
                        {"name": "worktree", "ok": True},
                        {"name": "usage_gate", "ok": False, "detail": "usage window unknown"},
                    ],
                })
            self.assertEqual(plan["improvement_task"]["category"], "usage")
            self.assertEqual(plan["improvement_task"]["record_outcome"], "recorded")
            self.assertTrue((Path(td) / "tasks.jsonl").is_file())

    def test_usage_gate_unknown_is_not_accepted_for_live_pilot(self):
        module = load_module()
        completed = subprocess.CompletedProcess([], 0, stdout="PROCEED (coach unavailable — gate skipped, not enforced)\n", stderr="")
        with patch.object(module.subprocess, "run", return_value=completed):
            ok, detail = module._usage_gate("codex")
        self.assertFalse(ok)
        self.assertIn("not confirm", detail)
        ok, detail = module._usage_gate("codex", allow_unmetered=True)
        self.assertTrue(ok)
        self.assertIn("explicit", detail)

        completed.stdout = "PROCEED - codex 5h창 잔여 N/A / 7d창 잔여 90%\n"
        with patch.object(module.subprocess, "run", return_value=completed):
            ok, _ = module._usage_gate("codex")
        self.assertTrue(ok)
        completed.stdout = "SKIP: codex 7d창 잔여 4%\n"
        with patch.object(module.subprocess, "run", return_value=completed):
            ok, detail = module._usage_gate("codex", allow_unmetered=True)
        self.assertFalse(ok)
        self.assertIn("SKIP", detail)
        self.assertFalse(module._usage_gate("agy")[0])
        self.assertTrue(module._usage_gate("agy", allow_unmetered=True)[0])

    def test_execution_streams_output_and_checks_resulting_diff(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._clean_repo(root)
            prompt = root / "prompt.txt"
            prompt.write_text("read-only canary\n", encoding="utf-8")
            fake_entrypoint = root / "fake-provider.sh"
            fake_entrypoint.write_text(
                "#!/bin/sh\n"
                "printf 'token=sk-fake-secret\\n'\n"
                "printf 'provider output\\n'\n"
                "touch \"$3/changed.txt\"\n",
                encoding="utf-8",
            )
            fake_entrypoint.chmod(0o700)
            with patch.object(module, "ENTRYPOINT", fake_entrypoint):
                result = module._run_provider("codex", prompt, repo, timeout=5)
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertTrue(result["ok"], result)
            self.assertIsNone(result["raw_output"])
            self.assertNotIn("sk-fake-secret", serialized)
            self.assertGreater(result["provider_output_bytes"], 0)
            self.assertEqual(len(result["provider_output_sha256"]), 64)
            self.assertIn("changed.txt", result["changed_files"])
            self.assertTrue(result["worktree_status_ok"])
            self.assertTrue(result["diff_check_ok"])


if __name__ == "__main__":
    unittest.main()
