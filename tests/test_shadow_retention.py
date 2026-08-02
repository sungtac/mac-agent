from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_shadow_event_store import ShadowEventStore  # noqa: E402
from edge_agent_shadow_maintenance import ShadowMaintenance, ShadowMaintenanceConfig  # noqa: E402


def event(name, created_at, status="terminal"):
    return {
        "event_id": name,
        "event_type": "test",
        "root_task_id": name,
        "created_at": created_at,
        "status": status,
        "message_identity_hash": f"hash-{name}",
    }


class ShadowRetentionTests(unittest.TestCase):
    def make(self, now=4_000_000):
        temp = tempfile.TemporaryDirectory()
        store = ShadowEventStore(Path(temp.name) / "shadow")
        maintenance = ShadowMaintenance(
            store,
            config=ShadowMaintenanceConfig(retention_days=30, retention_batch_size=2),
            clock=lambda: now,
        )
        return temp, store, maintenance

    def test_old_terminal_delivered_event_is_deleted(self):
        temp, store, maintenance = self.make()
        try:
            store.append(event("old", "1970-01-01T00:00:00Z"))
            preview = maintenance.retention_dry_run()
            self.assertEqual(preview["candidate_count"], 1)
            result = maintenance.retention_execute()
            self.assertEqual(result["deleted_count"], 1)
            self.assertEqual(store.count(), 0)
        finally:
            temp.cleanup()

    def test_active_event_is_preserved(self):
        temp, store, maintenance = self.make()
        try:
            store.append(event("active", "1970-01-01T00:00:00Z", "ACTIVE"))
            self.assertEqual(maintenance.retention_execute()["deleted_count"], 0)
            self.assertEqual(store.count(), 1)
        finally:
            temp.cleanup()

    def test_pending_outbox_is_preserved(self):
        temp, store, maintenance = self.make()
        try:
            store.append(event("pending", "1970-01-01T00:00:00Z", "PENDING_OUTBOX"), flush=False)
            self.assertEqual(maintenance.retention_execute()["deleted_count"], 0)
            self.assertEqual(store.pending_outbox_count(), 1)
        finally:
            temp.cleanup()

    def test_failed_unresolved_event_is_preserved(self):
        temp, store, maintenance = self.make()
        try:
            store.append(event("failed", "1970-01-01T00:00:00Z", "FAILED_UNRESOLVED"))
            self.assertEqual(maintenance.retention_execute()["deleted_count"], 0)
            self.assertEqual(store.count(), 1)
        finally:
            temp.cleanup()

    def test_retention_is_batched(self):
        temp, store, maintenance = self.make()
        try:
            for index in range(5):
                store.append(event(f"old-{index}", "1970-01-01T00:00:00Z"))
            result = maintenance.retention_execute()
            self.assertEqual(result["deleted_count"], 2)
            self.assertEqual(store.count(), 3)
            self.assertEqual(maintenance.retention_execute()["deleted_count"], 2)
            self.assertEqual(maintenance.retention_execute()["deleted_count"], 1)
        finally:
            temp.cleanup()

    def test_fake_clock_boundary_is_deterministic(self):
        now = 100 * 86400
        temp, store, maintenance = self.make(now=now)
        try:
            cutoff = now - 30 * 86400
            exact = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            store.append(event("exact", exact))
            self.assertEqual(maintenance.retention_dry_run()["candidate_count"], 0)
        finally:
            temp.cleanup()

    def test_maintenance_lock_serializes_retention(self):
        temp, store, maintenance = self.make()
        try:
            with maintenance._lock():
                with self.assertRaises(Exception):
                    maintenance.retention_execute()
        finally:
            temp.cleanup()

    def test_retention_dry_run_does_not_delete(self):
        temp, store, maintenance = self.make()
        try:
            store.append(event("old", "1970-01-01T00:00:00Z"))
            maintenance.command("retention-dry-run")
            self.assertEqual(store.count(), 1)
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
