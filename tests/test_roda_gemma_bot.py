import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

SPEC = importlib.util.spec_from_file_location("roda_bot", ROOT / "bin" / "roda-gemma-bot.py")
roda_bot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(roda_bot)


class RodaGemmaBotTests(unittest.TestCase):
    def test_render_unresolved_incidents_excludes_incidents_without_escalation_stage(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "health.json"
            state_file.write_text(json.dumps({
                "incidents": {
                    "routed": {
                        "incident_id": "routed", "role": "codex", "code": "execution_error",
                        "status": "open", "last_seen_at": 200, "escalation_stage": "awaiting_ack",
                    },
                    "unrouted": {
                        "incident_id": "unrouted", "role": "codex", "code": "unknown_noise",
                        "status": "open", "last_seen_at": 100, "escalation_stage": None,
                    },
                },
            }), encoding="utf-8")
            rendered = roda_bot._render_unresolved_incidents(state_file)
        self.assertIn("routed", rendered)
        self.assertNotIn("unrouted", rendered)
        self.assertIn("1건", rendered)

    def test_render_unresolved_incidents_all_filtered_reports_none(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "health.json"
            state_file.write_text(json.dumps({
                "incidents": {
                    "unrouted": {
                        "incident_id": "unrouted", "role": "codex", "code": "unknown_noise",
                        "status": "open", "last_seen_at": 100, "escalation_stage": None,
                    },
                },
            }), encoding="utf-8")
            rendered = roda_bot._render_unresolved_incidents(state_file)
        self.assertEqual(rendered, "현재 장애 원장에 미해결 사건이 없습니다.")
