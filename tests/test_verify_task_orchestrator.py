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
    def test_detect_scope_violation_ignores_empty_relevant_files(self):
        self.assertEqual(
            MODULE.detect_scope_violation(
                {"files_changed": ["unexpected.py"]}, {"relevant_files": []}
            ),
            [],
        )

    def test_detect_scope_violation_returns_files_outside_relevant_files(self):
        self.assertEqual(
            MODULE.detect_scope_violation(
                {"files_changed": ["allowed.py", "./unexpected.py"]},
                {"relevant_files": ["allowed.py"]},
            ),
            ["unexpected.py"],
        )

    def test_detect_scope_violation_returns_empty_when_all_files_are_relevant(self):
        self.assertEqual(
            MODULE.detect_scope_violation(
                {"files_changed": ["./allowed.py", "nested\\file.py"]},
                {"relevant_files": ["allowed.py", "nested/file.py"]},
            ),
            [],
        )

    def test_review_prompt_contains_completion_checklist_and_conditional_scope_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = MODULE.HostOrchestrator("task", Path(directory), Path(directory) / ".verify", 1, False)
            prompt = runner.review_prompt(
                {"relevant_files": ["allowed.py"]},
                {"files_changed": ["unexpected.py"], "content": "diff"},
                {"status": "not_run"},
                "plan",
                "claude",
            )
            self.assertIn("[하네스 사전검사: 범위 위반 감지됨]", prompt)
            self.assertIn("unexpected.py", prompt)
            for number in range(1, 6):
                self.assertIn(f"{number}.", prompt)

            clean_prompt = runner.review_prompt(
                {"relevant_files": ["allowed.py"]},
                {"files_changed": ["allowed.py"], "content": "diff"},
                {"status": "passed"},
                "plan",
                "claude",
            )
            self.assertNotIn("[하네스 사전검사: 범위 위반 감지됨]", clean_prompt)

    def test_match_open_finding_tracks_rename_with_same_symbol(self):
        previous = [{"id": "old-1", "file": "old.py", "location": "old.py:parse_value", "anchor": "def parse_value(value):"}]
        issue = {"file": "new.py", "location": "new.py:parse_value", "anchor": "def parse_value(value):"}
        git_result = subprocess.CompletedProcess([], 0, "R100\told.py\tnew.py\n", "")
        with patch.object(MODULE.subprocess, "run", return_value=git_result):
            matched = MODULE.match_open_finding(issue, previous, Path("."))
        self.assertEqual(matched["id"], "old-1")

    def test_match_open_finding_rejects_different_file_and_symbol(self):
        previous = [{"id": "old-1", "file": "old.py", "location": "old.py:parse_value", "anchor": "def parse_value(value):"}]
        issue = {"file": "other.py", "location": "other.py:render_page", "anchor": "def render_page(page):"}
        with patch.object(MODULE.subprocess, "run", side_effect=OSError("git unavailable")):
            self.assertIsNone(MODULE.match_open_finding(issue, previous, Path(".")))

    def test_match_open_finding_rejects_shared_generic_tokens_without_rename(self):
        previous = [{"id": "old-1", "file": "old.py", "location": "old.py:bug_fix", "anchor": "bug fix in parser"}]
        issue = {"file": "other.py", "location": "other.py:bug_fix", "anchor": "bug fix in renderer"}
        git_result = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(MODULE.subprocess, "run", return_value=git_result):
            self.assertIsNone(MODULE.match_open_finding(issue, previous, Path(".")))

    def test_match_open_finding_falls_back_when_git_fails(self):
        previous = [{"id": "old-1", "file": "same.py", "location": "same.py:parse_value", "anchor": "def parse_value(value):"}]
        issue = {"file": "same.py", "location": "same.py:parse_value", "anchor": "def parse_value(value):"}
        with patch.object(MODULE.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "git diff")):
            matched = MODULE.match_open_finding(issue, previous, Path("."))
        self.assertEqual(matched["id"], "old-1")

    def test_ensure_git_worktree_initializes_non_git_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

            result = MODULE.ensure_git_worktree(repo)

            self.assertEqual(result, {"initialized": True})
            self.assertTrue((repo / ".git").is_dir())
            self.assertTrue((repo / ".gitignore").is_file())
            completed = subprocess.run(
                ["git", "-C", str(repo), "log", "--oneline"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertTrue(completed.stdout.strip())

    def test_ensure_git_worktree_noop_on_existing_git_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            gitignore = repo / ".gitignore"
            original_contents = "keep-this-entry/\n"
            gitignore.write_text(original_contents, encoding="utf-8")

            result = MODULE.ensure_git_worktree(repo)

            self.assertEqual(result, {"initialized": False})
            self.assertEqual(gitignore.read_text(encoding="utf-8"), original_contents)

    def test_ensure_git_worktree_returns_error_on_non_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file_path = root / "plain-file"
            file_path.write_text("not a directory\n", encoding="utf-8")

            for path in (root / "missing", file_path):
                with self.subTest(path=path):
                    self.assertEqual(
                        MODULE.ensure_git_worktree(path),
                        {"initialized": False, "error": "not_a_directory"},
                    )

    def test_ensure_git_worktree_returns_error_when_git_init_fails(self):
        failed_init = subprocess.CompletedProcess(
            args=["git", "init"],
            returncode=1,
            stdout="",
            stderr="fatal: simulated init failure\n",
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            MODULE.subprocess, "run", return_value=failed_init
        ) as run:
            result = MODULE.ensure_git_worktree(Path(directory))

        self.assertEqual(result, {
            "initialized": False,
            "error": "git_init_failed",
            "detail": "fatal: simulated init failure",
        })
        run.assert_called_once()

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

    def test_claude_call_retries_once_and_returns_repaired_json(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            runner = MODULE.HostOrchestrator("task", repo, repo / ".verify", 1, False)
            runner.claude = "faux-claude"
            repaired = {"hasBlockingIssue": False, "issues": [], "checks": ["reviewed"]}
            with patch.object(
                runner,
                "process",
                side_effect=[(0, "prose response"), (0, json.dumps(repaired))],
            ) as process, patch.object(runner, "record_metric") as record_metric, patch.object(
                MODULE.importlib, "reload", side_effect=lambda module: module
            ):
                result = runner.claude_call("claude-review", "review prompt", expect_json=True)

            self.assertTrue(result["ok"])
            self.assertFalse(result["hasBlockingIssue"])
            self.assertEqual(result["checks"], ["reviewed"])
            self.assertEqual(process.call_count, 2)
            retry_call = process.call_args_list[1]
            self.assertEqual(retry_call.args[1], "claude-review-retry")
            self.assertEqual(retry_call.args[3], "claude-review-retry")
            self.assertIn("JSON 객체 하나만 출력", retry_call.args[0][-1])
            self.assertTrue(retry_call.args[0][-1].endswith("review prompt"))
            self.assertEqual(record_metric.call_count, 2)
            self.assertEqual(record_metric.call_args_list[1].args[1], "claude-review-retry")

    def test_claude_call_retries_only_once_when_json_is_still_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            runner = MODULE.HostOrchestrator("task", repo, repo / ".verify", 1, False)
            runner.claude = "faux-claude"
            with patch.object(
                runner,
                "process",
                side_effect=[(0, "first prose response"), (0, "second prose response")],
            ) as process, patch.object(runner, "record_metric") as record_metric, patch.object(
                MODULE.importlib, "reload", side_effect=lambda module: module
            ), patch.object(MODULE.HARNESS.ABSENCE_GUARD, "validate_provider_payload") as validate:
                result = runner.claude_call("claude-review", "review prompt", expect_json=True)

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "claude_json_result_missing")
            self.assertEqual(process.call_count, 2)
            self.assertEqual(record_metric.call_count, 2)
            validate.assert_called_once_with("first prose response")

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

    def test_dispatch_injects_absence_exemption_for_every_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            runner = MODULE.HostOrchestrator("task", repo, repo / ".verify", 1, False)
            runner.codex = "faux-codex"
            provider_output = json.dumps({
                "findings": "missing file; credential이 없습니다",
                "discovery_evidence": [{"source": "provider", "note": "preserve me"}],
            })
            with patch.object(runner, "process", return_value=(0, provider_output)), \
                 patch.object(runner, "record_metric"), \
                 patch.object(MODULE.importlib, "reload", side_effect=lambda module: module), \
                 patch.object(
                     MODULE.HARNESS.ABSENCE_GUARD,
                     "validate_provider_payload",
                     wraps=MODULE.HARNESS.ABSENCE_GUARD.validate_provider_payload,
                 ) as validate:
                results = [
                    runner.dispatch("codex", "role", "prompt", schema_kind)
                    for schema_kind in ("research", "review", "plan", "light-eval")
                ]

            self.assertEqual(validate.call_count, 4)
            for result, call in zip(results, validate.call_args_list):
                self.assertNotIn("dispatchFailed", result)
                self.assertEqual(result["discovery_evidence"][0]["source"], "provider")
                self.assertEqual(result["discovery_evidence"][1]["source"], "orchestrator_role_exempt")
                self.assertIs(call.args[0], result)

    def test_dispatch_records_host_policy_omission(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            runner = MODULE.HostOrchestrator("task", repo, repo / ".verify", 1, False)
            runner.agy = "faux-agy"
            with patch.object(runner, "process", return_value=(0, '{"findings": "missing file"}')), \
                 patch.object(runner, "record_metric"), \
                 patch.object(MODULE.importlib, "reload", side_effect=lambda module: module):
                omitted = runner.dispatch(
                    "agy", "role", "prompt", "research", {"relevant_files": ["missing.py"]}
                )

            self.assertEqual(omitted["discovery_evidence"][0]["source"], "host_policy_omission")
            self.assertEqual(omitted["discovery_evidence"][0]["omitted_files"], ["missing.py"])

    def test_execute_codex_injects_absence_exemption_before_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            runner = MODULE.HostOrchestrator("task", repo, repo / ".verify", 1, False)
            runner.codex = "faux-codex"
            provider_output = '{"ok": true, "message": "error: key가 없습니다: missing"}'
            with patch.object(runner, "process", return_value=(0, provider_output)), \
                 patch.object(runner, "record_metric"), \
                 patch.object(MODULE.importlib, "reload", side_effect=lambda module: module), \
                 patch.object(
                     MODULE.HARNESS.ABSENCE_GUARD,
                     "validate_provider_payload",
                     wraps=MODULE.HARNESS.ABSENCE_GUARD.validate_provider_payload,
                 ) as validate:
                result = runner.execute_codex("codex-execute", "prompt")

            self.assertNotIn("dispatchFailed", result)
            self.assertEqual(result["discovery_evidence"][0]["source"], "orchestrator_role_exempt")
            validate.assert_called_once_with(result)

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

    def test_run_full_stops_on_failed_tests_before_reviews(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            runner = MODULE.HostOrchestrator("task", repo, repo / ".verify", 1, False)
            verification = {"test_summary": {"status": "failed"}}
            with patch.object(runner, "dispatch") as dispatch, \
                 patch.object(runner, "claude_call") as claude_call:
                result = runner.run_full({}, verification)

            self.assertEqual(result, {
                "passed": False,
                "tier": "full",
                "error": "tests_failed",
                "test_summary": {"status": "failed"},
            })
            dispatch.assert_not_called()
            claude_call.assert_not_called()

    def test_run_full_treats_not_run_tests_as_non_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            runner = MODULE.HostOrchestrator("task", repo, repo / ".verify", 1, False)
            verification = {"test_summary": {"status": "not_run"}}
            review = {"hasBlockingIssue": False, "issues": [], "checks": []}
            with patch.object(runner, "dispatch", return_value=review) as dispatch, \
                 patch.object(runner, "claude_call", return_value={"ok": True, **review}) as claude_call:
                result = runner.run_full({}, verification)

            self.assertTrue(result["passed"])
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(dispatch.call_args.args[:2], ("agy", "antigravity-review-round-1"))
            claude_call.assert_called_once()

    def test_run_full_enters_independent_reviews_without_codex_self_check(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            runner = MODULE.HostOrchestrator("task", repo, repo / ".verify", 1, False)
            verification = {"test_summary": {"status": "passed"}}
            review = {"hasBlockingIssue": False, "issues": [], "checks": []}
            with patch.object(runner, "dispatch", return_value=review) as dispatch, \
                 patch.object(runner, "claude_call", return_value={"ok": True, **review}):
                result = runner.run_full({}, verification)

            self.assertTrue(result["passed"])
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(dispatch.call_args.args[:2], ("agy", "antigravity-review-round-1"))


if __name__ == "__main__":
    unittest.main()
