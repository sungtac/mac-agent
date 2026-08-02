from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_coordination import collect_report, wait_for_peer_results  # noqa: E402


class CoordinationCollectorTests(unittest.TestCase):
    def test_report_does_not_treat_missing_peer_as_completed(self):
        with patch("edge_agent_coordination.list_task_states", return_value=[]):
            report = collect_report(chat_id="-1", request="안티랑 로다 확인")
        self.assertIn("not_observed", report)
        self.assertIn("not confirmed", report)

    def test_matching_peer_results_are_collected(self):
        records = {
            "antigravity": [{"role": "antigravity", "status": "completed", "request_tail": "요청", "response_tail": "안티 결과", "updated_at": "now"}],
            "gemma": [{"role": "gemma", "status": "failed", "request_tail": "요청", "error": "Ollama 오류", "updated_at": "now"}],
        }
        with patch("edge_agent_coordination.list_task_states", side_effect=lambda *, role, chat_id: records[role]):
            report = collect_report(chat_id="-1", request="요청")
        self.assertIn("antigravity: status=completed", report)
        self.assertIn("gemma: status=failed", report)
        self.assertNotIn("Ollama 오류", report)

    def test_wait_times_out_with_honest_pending_report(self):
        with patch("edge_agent_coordination.list_task_states", return_value=[]):
            report = asyncio.run(wait_for_peer_results(chat_id="-1", request="요청", timeout_seconds=0.01, interval_seconds=0.01))
        self.assertIn("not_observed", report)

    def test_status_report_uses_service_state_and_omits_historical_response(self):
        records = {
            "antigravity": [{"status": "completed", "request_tail": "안티랑 로다는 현재 상태 확인", "response_tail": "오래된 Roda 실패 주장", "updated_at": "now"}],
            "gemma": [],
        }
        with patch("edge_agent_coordination.list_task_states", side_effect=lambda *, role, chat_id: records[role]), patch(
            "edge_agent_coordination._launchctl_state", return_value="running"
        ):
            report = collect_report(chat_id="-1", request="안티랑 로다는 현재 상태 확인")
        self.assertIn("service=running", report)
        self.assertIn("historical response omitted", report)
        self.assertNotIn("오래된 Roda 실패 주장", report)


if __name__ == "__main__":
    unittest.main()
