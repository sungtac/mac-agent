from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_shadow_maintenance import (  # noqa: E402
    HARD_LIMIT,
    NORMAL,
    SOFT_LIMIT,
    ShadowMaintenance,
    ShadowMaintenanceConfig,
)
from edge_agent_shadow_event_store import ShadowEventStore  # noqa: E402
from edge_agent_shadow_observer import ShadowObserver, ShadowObserverConfig  # noqa: E402


def metadata(index):
    return {
        "root_task_id": f"root-{index}", "revision_id": f"revision-{index}",
        "cross_bot_message_key": f"message-{index}", "telegram_update_key": f"update-{index}",
        "bot_id": "antigravity", "bot_role": "antigravity", "message_length": 0,
        "attachment_count": 0, "attachment_metadata": [], "body_hash": None,
        "body_hash_status": "UNKNOWN", "task_type": "UNKNOWN", "risk_level": "UNKNOWN",
        "observed_provider_role": "antigravity",
    }


class ShadowDiskBudgetTests(unittest.TestCase):
    def test_normal_state(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ShadowEventStore(Path(temp) / "shadow")
            maintenance = ShadowMaintenance(store, config=ShadowMaintenanceConfig(total_soft_limit_bytes=10_000_000, total_hard_limit_bytes=20_000_000))
            self.assertEqual(maintenance.disk_state(), NORMAL)

    def test_soft_limit_state(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ShadowEventStore(Path(temp) / "shadow")
            store.event_log_path.write_bytes(b"x" * 100)
            maintenance = ShadowMaintenance(store, config=ShadowMaintenanceConfig(total_soft_limit_bytes=50, total_hard_limit_bytes=10_000_000, jsonl_segment_max_bytes=1_000))
            self.assertEqual(maintenance.disk_state(), SOFT_LIMIT)

    def test_hard_limit_state(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ShadowEventStore(Path(temp) / "shadow")
            store.event_log_path.write_bytes(b"x" * 100)
            maintenance = ShadowMaintenance(store, config=ShadowMaintenanceConfig(total_soft_limit_bytes=50, total_hard_limit_bytes=90, jsonl_segment_max_bytes=1_000))
            self.assertEqual(maintenance.disk_state(), HARD_LIMIT)

    def test_observer_accounts_disk_budget_drop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "shadow"
            observer = ShadowObserver(ShadowObserverConfig(True, root, queue_size=8, total_soft_limit_bytes=1, total_hard_limit_bytes=2))
            self.assertTrue(observer.start())
            self.assertTrue(observer.enqueue(metadata(1)))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and observer.stats["dropped_disk_budget"] == 0:
                time.sleep(0.01)
            result = observer.stop(timeout=2)
            self.assertEqual(result["dropped_disk_budget"], 1)
            self.assertEqual(result["submitted"], result["dropped_disk_budget"])

    def test_budget_does_not_delete_unrelated_files(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ShadowEventStore(Path(temp) / "shadow")
            protected = store.root / "unrelated.marker"
            protected.write_text("keep", encoding="utf-8")
            maintenance = ShadowMaintenance(store, config=ShadowMaintenanceConfig(total_soft_limit_bytes=1, total_hard_limit_bytes=2))
            self.assertEqual(maintenance.disk_state(), HARD_LIMIT)
            self.assertTrue(protected.exists())

    def test_health_reports_disk_state_without_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ShadowEventStore(Path(temp) / "shadow")
            snapshot = ShadowMaintenance(store).health_snapshot()
            self.assertIn("disk_state", snapshot)
            self.assertNotIn("token", " ".join(snapshot).casefold())


if __name__ == "__main__":
    unittest.main()
