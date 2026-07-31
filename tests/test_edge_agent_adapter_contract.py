import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_adapter_contract import EdgeAgentRequest, EdgeAgentResult  # noqa: E402


class AdapterContractTests(unittest.TestCase):
    def request(self, **overrides):
        values = {
            "request_id": "req-1",
            "task_id": "task-1",
            "logical_session_id": "sess-1",
            "objective": "읽기 전용 상태를 확인한다",
            "source": "team_os",
            "allowed_files": ("docs/status.md",),
            "completion_gates": ("evidence_check",),
        }
        values.update(overrides)
        return EdgeAgentRequest(**values)

    def test_request_round_trip_is_data_only(self):
        restored = EdgeAgentRequest.from_dict(self.request().to_dict())
        self.assertEqual(restored.task_id, "task-1")
        self.assertFalse(restored.dispatch_allowed)
        self.assertEqual(restored.allowed_files, ("docs/status.md",))

    def test_high_risk_request_requires_approval_reference(self):
        with self.assertRaises(ValueError):
            self.request(risk_level="delete", dispatch_allowed=True)
        approved = self.request(risk_level="delete", dispatch_allowed=True, approval_ref="approval-1")
        self.assertTrue(approved.dispatch_allowed)

    def test_passed_result_requires_evidence(self):
        with self.assertRaises(ValueError):
            EdgeAgentResult(
                request_id="req-1",
                task_id="task-1",
                logical_session_id="sess-1",
                status="passed",
            )
        result = EdgeAgentResult(
            request_id="req-1",
            task_id="task-1",
            logical_session_id="sess-1",
            status="passed",
            verification_tier="light",
            event_idempotency_key="task-1::step-1",
            evidence_refs=("event://task-1/step-1",),
        )
        self.assertEqual(EdgeAgentResult.from_dict(result.to_dict()).status, "passed")

    def test_sensitive_material_is_rejected(self):
        with self.assertRaises(ValueError):
            self.request(objective="authorization: bearer secret")


if __name__ == "__main__":
    unittest.main()
