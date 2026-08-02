from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "edge_agent_improvement.py"
SPEC = importlib.util.spec_from_file_location("edge_agent_improvement_under_test", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ImprovementTaskTests(unittest.TestCase):
    def test_blocker_is_recorded_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            task, outcome = MODULE.record_blocker(
                root=directory,
                source="provider-pilot",
                category="usage",
                summary="usage gate returned no readable window",
                evidence=["blocked_checks=usage_gate", "provider=claude"],
                next_action="repair usage observation and rerun the plan",
                acceptance="plan reports a readable usage window",
            )
            self.assertEqual(outcome, "recorded")
            duplicate, duplicate_outcome = MODULE.record_blocker(
                root=directory,
                source="provider-pilot",
                category="usage",
                summary="usage gate returned no readable window",
                evidence=["blocked_checks=usage_gate", "provider=claude"],
                next_action="repair usage observation and rerun the plan",
                acceptance="plan reports a readable usage window",
            )
            self.assertEqual(task["task_id"], duplicate["task_id"])
            self.assertEqual(duplicate_outcome, "duplicate")
            rows = [json.loads(line) for line in (Path(directory) / "tasks.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)

    def test_sensitive_facts_are_rejected(self):
        with self.assertRaises(MODULE.ImprovementError):
            MODULE.record_blocker(
                root=tempfile.mkdtemp(),
                source="provider-pilot",
                category="usage",
                summary="usage failure",
                evidence=["api_key=secret-value"],
                next_action="repair usage observation",
                acceptance="gate passes",
            )

    def test_completed_task_requires_and_persists_revalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            task, _ = MODULE.record_blocker(
                root=directory,
                source="provider-pilot",
                category="usage",
                summary="usage gate returned no readable window",
                evidence=["blocked_checks=usage_gate"],
                next_action="repair usage observation and rerun the plan",
                acceptance="plan reports a readable usage window",
            )
            self.assertEqual(
                MODULE.mark_completed(task["task_id"], ["coach emits 5h and 7d windows", "Claude canary passed"], root=directory),
                "completed",
            )
            self.assertEqual(
                MODULE.mark_completed(task["task_id"], ["coach emits 5h and 7d windows", "Claude canary passed"], root=directory),
                "duplicate",
            )
            row = json.loads((Path(directory) / "tasks.jsonl").read_text().splitlines()[0])
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["revalidation_evidence"], ["coach emits 5h and 7d windows", "Claude canary passed"])

    def test_failed_harness_result_gets_actionable_task_without_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            result, outcome = MODULE.improvement_for_result(
                {"passed": False, "error": "preflight_failed", "blocking_issues": [{"description": "provider unavailable"}]},
                source="verify-task-harness",
                root=directory,
            )
            self.assertEqual(outcome, "recorded")
            self.assertEqual(result["category"], "process")
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("provider unavailable", serialized)
            self.assertIn("next_action", result)


if __name__ == "__main__":
    unittest.main()
