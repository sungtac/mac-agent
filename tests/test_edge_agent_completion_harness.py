import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import edge_agent_completion_harness as module  # noqa: E402


class CompletionHarnessTests(unittest.TestCase):
    def test_completed_improvement_tasks_are_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "improvements" / "tasks.jsonl"
            ledger.parent.mkdir()
            ledger.write_text(
                json.dumps({"task_id": "done", "status": "completed"}) + "\n"
                + json.dumps({"task_id": "open", "status": "queued"}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(module.unresolved_improvement_tasks(ledger.parent), [{"task_id": "open", "status": "queued"}])
            self.assertEqual(module.unresolved_improvement_tasks(ledger.parent, goal_id="goal-x"), [])

    def test_completion_is_blocked_until_all_domains_and_tasks_are_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "completion"
            improvements = Path(directory) / "improvements"
            store = module.CompletionStore(root)
            store.init_goal("goal-1", "OS upgrade")
            for domain in module.REQUIRED_DOMAINS:
                store.record_check("goal-1", domain, passed=True, evidence=[f"{domain}=pass"])
            with patch.dict(os.environ, {"EDGE_AGENT_IMPROVEMENT_ROOT": str(improvements)}):
                module.register_failure(
                    store,
                    "goal-1",
                    "regression",
                    blocker="new defect",
                    evidence=["test=failed"],
                    next_action="repair",
                )
                with self.assertRaises(module.CompletionError):
                    store.complete("goal-1", unresolved_tasks=module.unresolved_improvement_tasks(improvements))
                self.assertEqual(store.state()["status"], "open")

    def test_failed_checks_are_idempotent_and_canary_requires_four_roles_and_three_rounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "completion"
            store = module.CompletionStore(root)
            store.init_goal("goal-2", "OS upgrade")
            evidence_path = Path(directory) / "canary.json"
            evidence_path.write_text(json.dumps({"passed": True, "roles": ["claude", "codex", "antigravity"], "rounds": 3}), encoding="utf-8")
            passed, _ = module.canary_evidence_ok(evidence_path)
            self.assertFalse(passed)
            probes = {
                role: {"status": "verified_available", "method": "fresh_session_probe", "observed_at": "2026-08-02T10:00:00Z"}
                for role in ("claude", "codex", "antigravity", "roda")
            }
            evidence_path.write_text(json.dumps({"passed": True, "roles": ["claude", "codex", "antigravity", "roda"], "rounds": 3, "provider_probes": probes}), encoding="utf-8")
            passed, _ = module.canary_evidence_ok(evidence_path)
            self.assertTrue(passed)
            with patch.dict(os.environ, {"EDGE_AGENT_IMPROVEMENT_ROOT": str(Path(directory) / "improvements")}):
                first = module.register_failure(store, "goal-2", "telegram_canary", blocker="missing", evidence=["rounds=2"], next_action="run")
                second = module.register_failure(store, "goal-2", "telegram_canary", blocker="missing", evidence=["rounds=2"], next_action="run")
            self.assertEqual(first["task_id"], second["task_id"])
            self.assertEqual(len((Path(directory) / "improvements" / "tasks.jsonl").read_text().splitlines()), 1)

    def test_clean_repo_can_explicitly_preserve_named_artifacts(self):
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "preserved.zip").write_bytes(b"archive")
            self.assertTrue(module.check_clean_repo(repo, allow=["preserved.zip"])[0])

    def test_passing_domain_clears_its_stale_last_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = module.CompletionStore(Path(directory) / "completion")
            store.init_goal("goal-3", "OS upgrade")
            with patch.dict(os.environ, {"EDGE_AGENT_IMPROVEMENT_ROOT": str(Path(directory) / "improvements")}):
                module.register_failure(store, "goal-3", "canonical_parity", blocker="dirty", evidence=["repo=dirty"], next_action="commit")
            store.record_check("goal-3", "canonical_parity", passed=True, evidence=["repo=clean"])
            self.assertIsNone(store.state()["last_failure"])

    def test_unrelated_legacy_improvement_does_not_block_goal(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "improvements" / "tasks.jsonl"
            ledger.parent.mkdir()
            ledger.write_text(
                json.dumps({"task_id": "nano", "source": "nano-threshold-review", "status": "queued"}) + "\n"
                + json.dumps({"task_id": "completion", "source": "completion-harness", "status": "queued"}) + "\n",
                encoding="utf-8",
            )
            tasks = module.unresolved_improvement_tasks(ledger.parent, goal_id="goal-x")
            self.assertEqual([item["task_id"] for item in tasks], ["completion"])


if __name__ == "__main__":
    unittest.main()
