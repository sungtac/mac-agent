from pathlib import Path
import json
import os
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_shadow_event_store import ShadowEventStore  # noqa: E402
from edge_agent_shadow_maintenance import READ_ONLY_DEGRADED, ShadowMaintenance  # noqa: E402


class ShadowRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ShadowEventStore(Path(self.temp.name) / "shadow")
        self.maintenance = ShadowMaintenance(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_flushing_outbox_is_requeued(self):
        self.store.append({"event_id": "e", "event_type": "x", "root_task_id": "r"}, flush=False)
        with self.store.transaction() as connection:
            connection.execute("UPDATE jsonl_outbox SET status='FLUSHING'")
        self.assertEqual(self.maintenance.recover()["flushing_requeued"], 1)
        self.assertEqual(self.store.pending_outbox_count(), 1)

    def test_corrupt_segment_is_quarantined(self):
        self.store.append({"event_id": "e", "event_type": "x", "root_task_id": "r"})
        rotated = self.store.rotate_jsonl(max_bytes=1, force=True)
        segment = self.store.root / rotated["segment"]
        segment.write_text("not-json\n", encoding="utf-8")
        result = self.maintenance.recover()
        self.assertEqual(result["quarantined_segments"], [segment.name])
        self.assertFalse(segment.exists())

    def test_jsonl_corruption_does_not_break_sqlite(self):
        self.store.append({"event_id": "e", "event_type": "x", "root_task_id": "r"})
        with self.store.event_log_path.open("a", encoding="utf-8") as handle:
            handle.write("broken\n")
        self.assertEqual(self.store.count(), 1)

    def test_health_reports_pending_and_failed_outbox(self):
        self.store.append({"event_id": "e", "event_type": "x", "root_task_id": "r"}, flush=False)
        snapshot = self.store.health_snapshot()
        self.assertEqual(snapshot["pending_outbox"], 1)
        self.assertIn("active_connections", snapshot)

    def test_health_degraded_on_missing_root(self):
        root = self.store.root
        import shutil
        shutil.rmtree(root)
        snapshot = self.maintenance.health_snapshot()
        self.assertEqual(snapshot["disk_state"], "READ_ONLY_DEGRADED")

    def test_temp_files_are_not_treated_as_events(self):
        temporary = self.store.root / ".shadow-events.tmp"
        temporary.write_text("partial", encoding="utf-8")
        self.assertNotIn(temporary, self.store.jsonl_paths())

    def test_recovery_keeps_task_state_independent_of_hmac(self):
        event = {"event_id": "e", "event_type": "x", "root_task_id": "stable-root", "body_hash_status": "UNKNOWN"}
        self.store.append(event)
        self.assertEqual(self.store.get("e")["root_task_id"], "stable-root")


if __name__ == "__main__":
    unittest.main()
