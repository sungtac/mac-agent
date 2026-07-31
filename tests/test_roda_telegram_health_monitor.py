import importlib.util
import tempfile
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("health", Path(__file__).parents[1] / "bin" / "roda-telegram-health-monitor.py")
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)


class RodaHealthMonitorTests(unittest.TestCase):
    def test_classifies_provider_failures_without_raw_prompt(self):
        self.assertEqual(health.classify_line("[codex] 빈 응답"), "empty_response")
        self.assertEqual(health.classify_line("[claude] 처리 실패 error=https://secret.example/x"), "execution_error")
        self.assertIsNone(health.classify_line("처리 완료 duration=3s"))
        self.assertIsNone(health.classify_line("[codex] run_polling 종료 — 프로세스 재시작을 위해 종료합니다."))
        self.assertNotIn("https://", health._safe_detail("error https://secret.example/x"))

    def test_initial_poll_only_establishes_offsets(self):
        with tempfile.TemporaryDirectory() as td:
            original = health.TARGETS
            health.TARGETS = {"x": {"label": "missing", "log": Path(td) / "x.log"}}
            (Path(td) / "x.log").write_text("처리 실패 빈 응답\n", encoding="utf-8")
            state = {"initialized": False, "offsets": {}, "pending": {}, "alerted": {}}
            self.assertEqual(health.poll_once(state), [])
            self.assertTrue(state["initialized"])
            health.TARGETS = original

    def test_drops_legacy_polling_stopped_retry_without_dropping_real_failures(self):
        original = health.TARGETS
        health.TARGETS = {}
        try:
            state = {
                "initialized": True,
                "offsets": {},
                "pending": {},
                "alerted": {},
                "delivery_retry": [
                    {"role": "codex", "code": "polling_stopped"},
                    {"role": "codex", "code": "empty_response"},
                ],
            }

            alerts = health.poll_once(state)

            self.assertEqual([event["code"] for event in alerts], ["empty_response"])
            self.assertEqual(state["delivery_retry"], [])
        finally:
            health.TARGETS = original


if __name__ == "__main__":
    unittest.main()
