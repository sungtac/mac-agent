import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "verify-task-harness.py"
SPEC = importlib.util.spec_from_file_location("verify_task_harness", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class VerifyTaskHarnessTests(unittest.TestCase):
    def test_status_files_excludes_known_runtime_noise_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            paths = [
                "hooks-state/foo.json",
                "discord-bot/repo-locks/abc.lock",
                "jobs/xyz/tmp/file",
                "chrome/chrome-native-host",
                "plugins/.last_inuse_sweep",
                "last-update-result.json",
                ".last-update-result.json",
            ]
            for path in paths:
                target = repo / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("initial\n", encoding="utf-8")
            subprocess.run(["git", "add", *paths], cwd=repo, check=True)
            subprocess.run([
                "git", "-c", "user.email=test@example.com", "-c", "user.name=test",
                "commit", "-qm", "initial",
            ], cwd=repo, check=True)
            for path in paths:
                (repo / path).write_text("changed\n", encoding="utf-8")

            self.assertEqual(MODULE.status_files(repo), [])

    def test_status_files_still_reports_real_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            path = "skills/pptx/SKILL.md"
            target = repo / path
            target.parent.mkdir(parents=True)
            target.write_text("initial\n", encoding="utf-8")
            subprocess.run(["git", "add", path], cwd=repo, check=True)
            subprocess.run([
                "git", "-c", "user.email=test@example.com", "-c", "user.name=test",
                "commit", "-qm", "initial",
            ], cwd=repo, check=True)
            target.write_text("changed\n", encoding="utf-8")

            self.assertEqual(MODULE.status_files(repo), [path])

    def test_light_policy_is_deterministic_and_allows_code_paths(self):
        policy = MODULE.classify("작은 버그 수정", ["src/example.ts"])
        self.assertEqual(policy["track"], "light")

    def test_sensitive_and_dependency_paths_force_full(self):
        policy = MODULE.classify("의존성 업데이트", ["package.json"])
        self.assertEqual(policy["track"], "full")
        self.assertIn("new_dependency", policy["reasons"])

    def test_public_documentation_forces_full(self):
        policy = MODULE.classify("README 문서 수정", ["README.md"])
        self.assertEqual(policy["track"], "full")
        self.assertIn("sensitive_path", policy["reasons"])

    def test_test_commands_choose_declared_runners_without_assuming_pytest(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            tests = repo / "tests"
            tests.mkdir()
            (tests / "test_unit.py").write_text("import unittest\n\nclass TestUnit(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n", encoding="utf-8")
            (tests / "smoke.test.js").write_text("import test from 'node:test';\ntest('ok', () => {});\n", encoding="utf-8")

            commands = MODULE.test_commands(repo)

            self.assertEqual(commands, [
                "python3 -m unittest discover -s tests -p 'test_*.py'",
                "node --test tests/*.test.js",
            ])

    def test_run_tests_executes_all_detected_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            tests = repo / "tests"
            tests.mkdir()
            (tests / "test_unit.py").write_text("import unittest\n\nclass TestUnit(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n", encoding="utf-8")
            (tests / "smoke.test.js").write_text("import test from 'node:test';\ntest('ok', () => {});\n", encoding="utf-8")
            run_dir = repo / ".verify" / "runs" / "TASK-ALL-TESTS"

            summary = MODULE.run_tests(repo, run_dir)

            self.assertEqual(summary["status"], "passed")
            self.assertEqual(len(summary["results"]), 2)
            self.assertTrue(all(item["status"] == "passed" for item in summary["results"]))

    def test_failed_direct_harness_tests_create_improvement_task(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            tests = repo / "tests"
            tests.mkdir()
            (tests / "test_unit.py").write_text(
                "import unittest\n\nclass TestUnit(unittest.TestCase):\n"
                "    def test_fail(self):\n        self.assertTrue(False)\n",
                encoding="utf-8",
            )
            run_dir = repo / ".verify" / "runs" / "TASK-FAILED-TESTS"
            with patch.dict(MODULE.os.environ, {"EDGE_AGENT_IMPROVEMENT_ROOT": str(Path(directory) / "improvements")}, clear=False):
                summary = MODULE.run_tests(repo, run_dir)
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["improvement_task"]["status"], "queued")
            self.assertTrue((run_dir / "improvement-task.json").is_file())

    def test_test_command_argv_expands_globs_without_shell_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            tests = repo / "tests"
            tests.mkdir()
            (tests / "smoke.test.js").write_text("", encoding="utf-8")

            argv = MODULE.test_command_argv("node --test tests/*.test.js", repo)

            self.assertEqual(argv, ["node", "--test", "tests/smoke.test.js"])
            self.assertEqual(MODULE.test_command_argv("node --test tests/*.missing.js", repo), [
                "node", "--test", "tests/*.missing.js",
            ])

    def test_agent_bridge_metric_keeps_unknown_usage_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "TASK-METRICS"
            with patch.dict(MODULE.os.environ, {
                "VERIFY_AGENT": "claude",
                "VERIFY_ROLE": "harness-snapshot-tests",
                "VERIFY_MODEL": "sonnet",
                "VERIFY_EFFORT": "medium",
                "VERIFY_PACKAGE_BYTES": "400",
            }, clear=False):
                MODULE.append_agent_bridge_metric(run_dir)

            record = json.loads((run_dir / "metrics.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["agent"], "claude")
            self.assertEqual(record["package_bytes"], 400)
            self.assertIsNone(record["input_tokens"])
            self.assertIsNone(record["output_tokens"])

    def test_failure_summary_ignores_passing_test_names(self):
        output = "✔ fails closed for malformed input\n✔ normal test\n"
        self.assertEqual(MODULE.summarize_failure_lines(output), [])
        self.assertEqual(MODULE.summarize_failure_lines("✖ actual failure\n"), ["✖ actual failure"])

    def test_absence_claim_gate_requires_discovery_evidence(self):
        with self.assertRaises(MODULE.ABSENCE_GUARD.UnsupportedAbsenceClaim):
            MODULE.ABSENCE_GUARD.validate_provider_payload({"summary": "config is not configured"})
        MODULE.ABSENCE_GUARD.validate_provider_payload({
            "summary": "not configured in searched scope",
            "discovery_evidence": {"searched_scopes": ["environment", "launch agents"]},
        })

    def test_failed_result_is_forced_into_improvement_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            with patch.dict(MODULE.os.environ, {"EDGE_AGENT_IMPROVEMENT_ROOT": str(Path(directory) / "improvements")}, clear=False):
                result = MODULE.ensure_improvement_task({"passed": False, "error": "usage_gate_unknown"}, run_dir)
            self.assertEqual(result["improvement_task"]["status"], "queued")
            self.assertTrue((run_dir / "improvement-task.json").is_file())
            self.assertIn("task_id", result["improvement_task"])

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

    def test_init_includes_matching_existing_test_in_allowed_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "example.py").write_text("def greeting(name):\n    return name\n", encoding="utf-8")
            (repo / "test_example.py").write_text("def test_greeting():\n    assert True\n", encoding="utf-8")
            subprocess.run(["git", "add", "example.py", "test_example.py"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-qm", "initial"], cwd=repo, check=True)
            run_dir = repo / ".verify" / "runs" / "TASK-TEST-HANDOFF"

            init = MODULE.init_run(repo, "example.py 수정 및 테스트 보강", run_dir, False)

            self.assertIn("example.py", init["relevant_files"])
            self.assertIn("test_example.py", init["relevant_files"])


if __name__ == "__main__":
    unittest.main()
