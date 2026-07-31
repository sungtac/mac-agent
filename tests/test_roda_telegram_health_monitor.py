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

    def test_recovery_watch_reports_reprocess_success(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "x.log"
            log.write_text("기존 로그\n", encoding="utf-8")
            original_targets = health.TARGETS
            original_running = health._service_running
            health.TARGETS = {"x": {"label": "present", "log": log}}
            health._service_running = lambda label: True
            state = {
                "initialized": False,
                "offsets": {},
                "pending": {},
                "alerted": {},
                "delivery_retry": [],
                "repair_results": {},
                "recovery_watch": {
                    "abc123": {
                        "role": "x",
                        "status": "awaiting_reprocess",
                        "deadline": 9999,
                        "notified": False,
                    }
                },
            }
            health.poll_once(state, now=100)
            with log.open("a", encoding="utf-8") as handle:
                handle.write("처리 시작 chat=test\n처리 완료 chat=test duration=1s\n")
            alerts = health.poll_once(state, now=101)
            self.assertEqual(state["recovery_watch"]["abc123"]["status"], "completed_success")
            self.assertEqual(alerts[0]["kind"], "recovery_result")
            health.TARGETS = original_targets
            health._service_running = original_running

    def test_isolated_end_to_end_detection_repair_and_recovery_alert(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "x.log"
            state_file = root / "state.json"
            log.write_text("기존 로그\n", encoding="utf-8")
            original = {
                "TARGETS": health.TARGETS,
                "STATE_FILE": health.STATE_FILE,
                "_service_running": health._service_running,
                "_run_codex_repair": health._run_codex_repair,
                "_send_alert": health._send_alert,
            }
            alerts = []
            repairs = []
            health.TARGETS = {"x": {"label": "present", "log": log}}
            health.STATE_FILE = state_file
            health._service_running = lambda label: True
            health._run_codex_repair = lambda event: repairs.append(event["code"]) or "Codex 자동 수정·main 병합·x 서비스 재기동 완료."
            health._send_alert = alerts.append
            try:
                state = {"initialized": False, "offsets": {}, "pending": {}, "alerted": {}, "delivery_retry": [], "repair_results": {}, "recovery_watch": {}}
                health._process_cycle(state)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("처리 실패 빈 응답\n")
                health._process_cycle(state)
                self.assertEqual(repairs, ["empty_response"])
                self.assertEqual(len(alerts), 1)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("처리 시작 chat=test\n처리 완료 chat=test duration=1s\n")
                health._process_cycle(state)
                self.assertEqual(len(alerts), 2)
                self.assertIn("재처리 성공", alerts[1])
            finally:
                for name, value in original.items():
                    setattr(health, name, value)

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
