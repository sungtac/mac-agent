from pathlib import Path
import json
import multiprocessing
import os
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_shadow_event_store import ShadowEventStore  # noqa: E402
from edge_agent_shadow_maintenance import ShadowMaintenance, ShadowMaintenanceConfig  # noqa: E402


def rotate_in_child(root, output):
    child_store = ShadowEventStore(root)
    output.put(child_store.rotate_jsonl(max_bytes=1, force=True, now=1_700_000_010))


class ShadowRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ShadowEventStore(Path(self.temp.name) / "shadow")

    def tearDown(self):
        self.temp.cleanup()

    def event(self, name):
        return {"event_id": name, "event_type": "rotation", "root_task_id": name, "message_identity_hash": name}

    def test_size_rotation_creates_closed_segment(self):
        self.store.append(self.event("one"))
        result = self.store.rotate_jsonl(max_bytes=1, now=1_700_000_000)
        self.assertTrue(result["rotated"])
        self.assertTrue((self.store.root / result["segment"]).exists())
        self.assertTrue(self.store.event_log_path.exists())

    def test_time_or_explicit_rotation_works_below_size(self):
        self.store.append(self.event("one"))
        result = self.store.rotate_jsonl(max_bytes=10_000_000, force=True, now=1_700_000_001)
        self.assertTrue(result["rotated"])

    def test_time_boundary_rotation_uses_configured_age(self):
        self.store.append(self.event("time"))
        os.utime(self.store.event_log_path, (1, 1))
        maintenance = ShadowMaintenance(
            self.store,
            config=ShadowMaintenanceConfig(jsonl_rotation_interval_seconds=10),
            clock=lambda: 100,
        )
        self.assertTrue(maintenance.rotate_if_needed()["rotated"])

    def test_rotation_is_atomic_from_reader_view(self):
        self.store.append(self.event("one"))
        result = self.store.rotate_jsonl(max_bytes=1, now=1_700_000_002)
        closed = self.store.root / result["segment"]
        self.assertEqual(len(closed.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(self.store.read_event_log(), ([], []))

    def test_rotation_preserves_jsonl_permissions(self):
        self.store.append(self.event("one"))
        result = self.store.rotate_jsonl(max_bytes=1, force=True)
        self.assertEqual(result["segment"], result["segment"])
        self.assertEqual(os.stat(self.store.root / result["segment"]).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.store.event_log_path).st_mode & 0o777, 0o600)

    def test_closed_segment_is_listed(self):
        self.store.append(self.event("one"))
        self.store.rotate_jsonl(max_bytes=1, force=True)
        self.assertEqual(len(self.store.closed_jsonl_paths()), 1)

    def test_two_process_rotations_leave_valid_segments(self):
        self.store.append(self.event("multi"))
        context = multiprocessing.get_context("fork")
        output = context.Queue()
        processes = [context.Process(target=rotate_in_child, args=(self.store.root, output)) for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        while not output.empty():
            output.get()
        for path in self.store.closed_jsonl_paths():
            for line in path.read_text(encoding="utf-8").splitlines():
                self.assertIsInstance(json.loads(line), dict)

    def test_corrupt_closed_segment_is_quarantined(self):
        self.store.append(self.event("one"))
        result = self.store.rotate_jsonl(max_bytes=1, force=True)
        path = self.store.root / result["segment"]
        path.write_text("{broken\n", encoding="utf-8")
        found = self.store.quarantine_corrupt_segments()
        self.assertEqual(found, [path.name])
        self.assertTrue((self.store.root / "quarantine" / path.name).exists())

    def test_jsonl_retention_never_removes_active_segment(self):
        self.store.append(self.event("one"))
        maintenance = ShadowMaintenance(
            self.store,
            config=ShadowMaintenanceConfig(jsonl_retention_days=1),
            clock=lambda: 10_000_000,
        )
        self.assertNotIn(self.store.event_log_path.name, maintenance.jsonl_retention_dry_run()["segments"])

    def test_jsonl_retention_removes_only_old_closed_segments(self):
        self.store.append(self.event("one"))
        result = self.store.rotate_jsonl(max_bytes=1, force=True)
        closed = self.store.root / result["segment"]
        os.utime(closed, (1, 1))
        maintenance = ShadowMaintenance(
            self.store,
            config=ShadowMaintenanceConfig(jsonl_retention_days=1),
            clock=lambda: 10 * 86400,
        )
        result = maintenance.jsonl_retention_execute()
        self.assertEqual(result["deleted_segments"], [closed.name])


if __name__ == "__main__":
    unittest.main()
