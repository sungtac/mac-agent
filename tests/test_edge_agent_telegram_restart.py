import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "edge-agent-telegram-restart.py"
SPEC = importlib.util.spec_from_file_location("edge_agent_telegram_restart", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class TelegramRestartTests(unittest.TestCase):
    def test_stale_unfinished_request_after_restart_is_not_active(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "stderr.log"
            log.write_text(
                "[2026-08-01T00:00:00Z] 처리 시작 chat=1\n"
                "[2026-08-01T00:01:00Z] Starting direct Telegram antigravity bot\n",
                encoding="utf-8",
            )
            self.assertFalse(MODULE.request_is_active(log))

    def test_active_request_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "stderr.log"
            log.write_text(
                "[2026-08-01T00:00:00Z] Starting direct Telegram antigravity bot\n"
                "[2026-08-01T00:01:00Z] 처리 시작 chat=1\n",
                encoding="utf-8",
            )
            self.assertTrue(MODULE.request_is_active(log))

    def test_role_specific_request_markers_are_supported(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "stderr.log"
            log.write_text(
                "connected as @sukja_hwpx_helper_bot\n"
                "request started chat=-100\n",
                encoding="utf-8",
            )
            self.assertTrue(MODULE.request_is_active(
                log,
                "connected as @",
                "request started",
                ("request completed", "request failed"),
            ))

    def test_restart_refuses_to_kill_active_request(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "stderr.log"
            log.write_text("처리 시작 chat=1\n", encoding="utf-8")
            original = MODULE.TARGETS
            MODULE.TARGETS = {"x": {"label": "com.example.x", "log": log}}
            try:
                with self.assertRaises(TimeoutError):
                    MODULE.restart("x", drain_seconds=0, sleep=lambda _: None)
            finally:
                MODULE.TARGETS = original

    def test_marker_is_written_and_cleared_around_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "stderr.log"
            marker = root / "maintenance.json"
            log.write_text("Starting direct Telegram antigravity bot\n", encoding="utf-8")
            original_targets = MODULE.TARGETS
            original_marker = MODULE.MAINTENANCE_FILE
            MODULE.TARGETS = {"x": {"label": "com.example.x", "log": log}}
            MODULE.MAINTENANCE_FILE = marker
            calls = []

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            def runner(argv, **kwargs):
                calls.append(argv)
                if "kickstart" in argv:
                    with log.open("a", encoding="utf-8") as handle:
                        handle.write("Starting direct Telegram antigravity bot\n")
                return Result()

            try:
                with patch.object(MODULE, "service_running", return_value=True):
                    MODULE.restart("x", drain_seconds=0, runner=runner, sleep=lambda _: None)
                self.assertIn("kickstart", calls[0])
                self.assertFalse(marker.exists())
            finally:
                MODULE.TARGETS = original_targets
                MODULE.MAINTENANCE_FILE = original_marker

    def test_stop_drains_and_boots_out_service(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "stderr.log"
            marker = root / "maintenance.json"
            log.write_text("Starting direct Telegram codex bot\n", encoding="utf-8")
            original_targets = MODULE.TARGETS
            original_marker = MODULE.MAINTENANCE_FILE
            MODULE.TARGETS = {"x": {"label": "com.example.x", "log": log}}
            MODULE.MAINTENANCE_FILE = marker
            calls = []

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            def runner(argv, **kwargs):
                calls.append(argv)
                return Result()

            try:
                with patch.object(MODULE, "service_running", side_effect=[True, False]):
                    MODULE.stop("x", drain_seconds=0, runner=runner, sleep=lambda _: None)
                self.assertIn("bootout", calls[0])
                self.assertFalse(marker.exists())
            finally:
                MODULE.TARGETS = original_targets
                MODULE.MAINTENANCE_FILE = original_marker


if __name__ == "__main__":
    unittest.main()
