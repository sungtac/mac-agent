import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "verify-task-orchestrator.py"
SPEC = importlib.util.spec_from_file_location("verify_task_orchestrator", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class VerifyTaskOrchestratorTests(unittest.TestCase):
    def test_antigravity_prompt_contains_bounded_evidence_and_forbids_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            source = repo / "example.ts"
            source.write_text("export const value = 1;\n", encoding="utf-8")
            runner = MODULE.HostOrchestrator("example.ts 수정", repo, repo / ".verify", 1, False)
            prompt, _ = runner.headless_evidence_prompt(
                "조사 결과를 JSON으로 반환하라.",
                {"relevant_files": ["example.ts"], "rules_text": "- 테스트를 실행하지 마라."},
            )
            self.assertIn("HEADLESS EVIDENCE CONTRACT", prompt)
            self.assertIn("Bash, command, git, Read, Grep", prompt)
            self.assertIn("export const value = 1;", prompt)
            self.assertIn("APPLICABLE RULE TEXT", prompt)

    def test_antigravity_review_omits_duplicate_file_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / "example.ts").write_text("export const value = 1;\n", encoding="utf-8")
            runner = MODULE.HostOrchestrator("example.ts 수정", repo, repo / ".verify", 1, False)
            prompt, _ = runner.headless_evidence_prompt(
                "diff를 검토하라.",
                {"relevant_files": ["example.ts"]},
                include_files=False,
            )
            self.assertNotIn("export const value = 1;", prompt)
            self.assertIn("OMITTED BY HOST POLICY", prompt)

    def test_nested_provider_result_unwraps_json_without_api_usage(self):
        result, usage = MODULE.nested_provider_result({
            "usage": {"input_tokens": 12, "output_tokens": 4},
            "result": '{"verdict":"pass"}',
        })
        self.assertEqual(result, {"verdict": "pass"})
        self.assertEqual(usage["input_tokens"], 12)

    def test_last_json_keeps_provider_envelope_instead_of_nested_array(self):
        value = MODULE.last_json(
            'provider banner {"focus":"research","evidence":[{"source":"example.py"}],"testImplications":["make test"]}'
        )
        self.assertEqual(value["focus"], "research")
        self.assertEqual(value["testImplications"], ["make test"])

    def test_dry_run_uses_host_harness_and_subscription_cli_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "example.ts").write_text("export const value = 1;\n", encoding="utf-8")
            subprocess.run(["git", "add", "example.ts"], cwd=repo, check=True)
            subprocess.run([
                "git", "-c", "user.email=test@example.com", "-c", "user.name=test",
                "commit", "-qm", "initial",
            ], cwd=repo, check=True)

            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            for name in ("codex", "agy"):
                executable = fake_bin / name
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)

            run_dir = repo / ".verify" / "runs" / "DRY-RUN"
            env = {**os.environ, "CODEX_BIN": str(fake_bin / "codex"), "AGY_BIN": str(fake_bin / "agy")}
            history_file = Path(directory) / "verify-history.jsonl"
            env["VERIFY_TASK_HISTORY_FILE"] = str(history_file)
            completed = subprocess.run([
                "python3", str(SCRIPT), "--task", "example.ts 수정", "--cwd", str(repo),
                "--run-dir", str(run_dir), "--dry-run",
            ], cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)

            payload = json.loads(completed.stdout)
            self.assertTrue(payload["finalVerdict"]["dry_run"])
            self.assertEqual(payload["finalVerdict"]["track"], "light")
            self.assertTrue((run_dir / "task.json").is_file())
            self.assertTrue((run_dir / "metrics.jsonl").is_file())
            history = [json.loads(line) for line in history_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["schema"], "edge_agent.verify_task_history.v1")
            self.assertTrue(history[0]["passed"])
            self.assertNotIn("example.ts 수정", history[0])

    def test_corrupt_history_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            run_dir = repo / ".verify" / "runs" / "CORRUPT"
            history_file = Path(directory) / "verify-history.jsonl"
            history_file.write_text('{"ok":true}\nnot-json\n', encoding="utf-8")
            improvement_root = Path(directory) / "improvements"
            runner = MODULE.HostOrchestrator("검증", repo, run_dir, 1, False)
            with patch.dict(os.environ, {
                "VERIFY_TASK_HISTORY_FILE": str(history_file),
                "EDGE_AGENT_IMPROVEMENT_ROOT": str(improvement_root),
            }):
                result = {"passed": True, "dry_run": True, "run_dir": str(run_dir)}
                runner.persist_result(result)
            self.assertFalse(result["passed"])
            self.assertEqual(result["error"], "history_persist_failed")
            self.assertIn("improvement_task", result)
            self.assertEqual(len(history_file.read_text(encoding="utf-8").splitlines()), 2)

    def test_execute_codex_reloads_absence_guard_before_validating(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            runner = MODULE.HostOrchestrator("task", repo, repo / ".verify", 1, False)
            runner.codex = "faux-codex"
            with patch.object(runner, "process", return_value=(0, '{"ok": true, "message": "done"}')), \
                 patch.object(runner, "record_metric"), \
                 patch.object(MODULE.importlib, "reload") as mock_reload:
                runner.execute_codex("role", "prompt")
            mock_reload.assert_called_once_with(MODULE.HARNESS.ABSENCE_GUARD)

    def test_claude_call_reloads_absence_guard_before_validating(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            runner = MODULE.HostOrchestrator("task", repo, repo / ".verify", 1, False)
            runner.claude = "faux-claude"
            with patch.object(runner, "process", return_value=(0, '{"ok": true, "message": "done"}')), \
                 patch.object(runner, "record_metric"), \
                 patch.object(MODULE.importlib, "reload") as mock_reload:
                runner.claude_call("role", "prompt")
            mock_reload.assert_called_once_with(MODULE.HARNESS.ABSENCE_GUARD)

    def test_dispatch_reloads_absence_guard_before_validating(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            runner = MODULE.HostOrchestrator("task", repo, repo / ".verify", 1, False)
            runner.codex = "faux-codex"
            with patch.object(runner, "process", return_value=(0, '{"ok": true}')), \
                 patch.object(runner, "record_metric"), \
                 patch.object(MODULE.importlib, "reload") as mock_reload:
                runner.dispatch("codex", "role", "prompt", "review")
            mock_reload.assert_called_once_with(MODULE.HARNESS.ABSENCE_GUARD)

    def test_execute_codex_survives_a_broken_reload_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            runner = MODULE.HostOrchestrator("task", repo, repo / ".verify", 1, False)
            runner.codex = "faux-codex"
            with patch.object(runner, "process", return_value=(0, '{"ok": true, "message": "done"}')), \
                 patch.object(runner, "record_metric"), \
                 patch.object(MODULE.importlib, "reload", side_effect=SyntaxError("guard mid-edit")):
                result = runner.execute_codex("role", "prompt")
            self.assertTrue(result.get("dispatchFailed"))
            self.assertEqual(result.get("error"), "absence_guard_reload_failed")

    def test_claude_call_survives_a_broken_reload_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            runner = MODULE.HostOrchestrator("task", repo, repo / ".verify", 1, False)
            runner.claude = "faux-claude"
            with patch.object(runner, "process", return_value=(0, '{"ok": true, "message": "done"}')), \
                 patch.object(runner, "record_metric"), \
                 patch.object(MODULE.importlib, "reload", side_effect=SyntaxError("guard mid-edit")):
                result = runner.claude_call("role", "prompt")
            self.assertFalse(result.get("ok"))
            self.assertEqual(result.get("error"), "absence_guard_reload_failed")

    def test_dispatch_survives_a_broken_reload_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            runner = MODULE.HostOrchestrator("task", repo, repo / ".verify", 1, False)
            runner.codex = "faux-codex"
            with patch.object(runner, "process", return_value=(0, '{"ok": true}')), \
                 patch.object(runner, "record_metric"), \
                 patch.object(MODULE.importlib, "reload", side_effect=SyntaxError("guard mid-edit")):
                result = runner.dispatch("codex", "role", "prompt", "review")
            self.assertTrue(result.get("dispatchFailed"))
            self.assertEqual(result.get("dispatchFailureReason"), "absence_guard_reload_failed")


if __name__ == "__main__":
    unittest.main()
