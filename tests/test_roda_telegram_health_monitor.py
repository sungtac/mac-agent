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
                self.assertEqual(len(alerts), 2)
                self.assertIn("Codex 자동복구 시작", alerts[0])
                self.assertIn("Codex 자동복구 결과", alerts[1])
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("처리 시작 chat=test\n처리 완료 chat=test duration=1s\n")
                health._process_cycle(state)
                self.assertEqual(len(alerts), 3)
                self.assertIn("재처리 성공", alerts[2])
            finally:
                for name, value in original.items():
                    setattr(health, name, value)

    def test_repair_result_only_instructs_reprocess_after_success(self):
        event = {"role": "claude", "code": "service_down"}
        failed = health._format_repair_result(event, "Codex 진단 실행 실패: TimeoutExpired")
        self.assertIn("미완료/실패", failed)
        self.assertIn("지시하지 않습니다", failed)
        self.assertNotIn("@edgeai_stk_bot", failed)
        succeeded = health._format_repair_result(event, "Codex 자동 수정·main 병합·claude 서비스 재기동 완료.")
        self.assertIn("@edgeai_stk_bot", succeeded)
        self.assertIn("다시 처리하세요", succeeded)

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

    def test_planned_restart_suppresses_service_down(self):
        with tempfile.TemporaryDirectory() as td:
            original = {
                "TARGETS": health.TARGETS,
                "MAINTENANCE_FILE": health.MAINTENANCE_FILE,
                "_service_running": health._service_running,
            }
            try:
                log = Path(td) / "x.log"
                marker = Path(td) / "maintenance.json"
                log.write_text("기존 로그\n", encoding="utf-8")
                marker.write_text('{"version": 1, "roles": {"x": {"expires_at": 200}}}', encoding="utf-8")
                health.TARGETS = {"x": {"label": "present", "log": log}}
                health.MAINTENANCE_FILE = marker
                health._service_running = lambda label: False
                state = {"initialized": True, "offsets": {"x": len("기존 로그\n".encode())}, "pending": {}, "alerted": {}}
                alerts = health.poll_once(state, now=100)
                self.assertEqual(alerts, [])
                self.assertEqual(state["service_down_since"], {})
            finally:
                for name, value in original.items():
                    setattr(health, name, value)

    def test_service_down_requires_grace_period(self):
        with tempfile.TemporaryDirectory() as td:
            original = {
                "TARGETS": health.TARGETS,
                "MAINTENANCE_FILE": health.MAINTENANCE_FILE,
                "_service_running": health._service_running,
            }
            try:
                log = Path(td) / "x.log"
                marker = Path(td) / "maintenance.json"
                log.write_text("기존 로그\n", encoding="utf-8")
                health.TARGETS = {"x": {"label": "present", "log": log}}
                health.MAINTENANCE_FILE = marker
                health._service_running = lambda label: False
                state = {"initialized": True, "offsets": {"x": len("기존 로그\n".encode())}, "pending": {}, "alerted": {}}
                self.assertEqual(health.poll_once(state, now=100), [])
                self.assertEqual(health.poll_once(state, now=100 + health.SERVICE_DOWN_GRACE_SECONDS - 1), [])
                alerts = health.poll_once(state, now=100 + health.SERVICE_DOWN_GRACE_SECONDS)
                self.assertEqual([event["code"] for event in alerts], ["service_down"])
            finally:
                for name, value in original.items():
                    setattr(health, name, value)


if __name__ == "__main__":
    unittest.main()
