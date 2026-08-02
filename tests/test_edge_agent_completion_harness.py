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
            evidence_path.write_text(json.dumps({"passed": True, "roles": ["claude", "codex", "antigravity", "roda"], "rounds": 3}), encoding="utf-8")
            passed, _ = module.canary_evidence_ok(evidence_path)
            self.assertTrue(passed)
            with patch.dict(os.environ, {"EDGE_AGENT_IMPROVEMENT_ROOT": str(Path(directory) / "improvements")}):
                first = module.register_failure(store, "goal-2", "telegram_canary", blocker="missing", evidence=["rounds=2"], next_action="run")
                second = module.register_failure(store, "goal-2", "telegram_canary", blocker="missing", evidence=["rounds=2"], next_action="run")
            self.assertEqual(first["task_id"], second["task_id"])
            self.assertEqual(len((Path(directory) / "improvements" / "tasks.jsonl").read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
