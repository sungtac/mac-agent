from pathlib import Path
import json
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_shadow_event_store import ShadowEventStore  # noqa: E402


def event(event_id="event-1"):
    return {
        "event_id": event_id,
        "event_type": "ingress_observed",
        "root_task_id": "root-1",
        "revision_id": "revision-1",
        "body_hash": None,
        "body_hash_status": "UNKNOWN",
        "attachment_hashes": [],
    }


class ShadowOutboxRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "shadow"
        self.store = ShadowEventStore(self.root)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_event_and_outbox_are_created_together_without_jsonl_write(self):
        self.store.append(event(), flush=False)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.pending_outbox_count(), 1)
        self.assertFalse(self.store.event_log_path.exists())

    def test_jsonl_failure_leaves_authoritative_event_and_pending_outbox(self):
        self.store.append(event(), flush=False)
        original = self.store._append_jsonl_if_missing
        self.store._append_jsonl_if_missing = lambda data: (_ for _ in ()).throw(OSError("disk full"))
        result = self.store.flush_pending()
        self.store._append_jsonl_if_missing = original
        self.assertEqual(result["errors"], 1)
        status = self.store.outbox_status("event-1")
        self.assertEqual(status["status"], "PENDING")
        self.assertEqual(status["attempt_count"], 1)
        self.assertEqual(self.store.count(), 1)

    def test_restart_reprocesses_pending_outbox(self):
        self.store.append(event(), flush=False)
        restarted = ShadowEventStore(self.root)
        result = restarted.flush_pending()
        self.assertEqual(result["written"], 1)
        self.assertEqual(restarted.outbox_status("event-1")["status"], "DELIVERED")
        self.assertEqual(len(restarted.read_event_log()[0]), 1)

    def test_duplicate_event_never_duplicates_jsonl(self):
        self.store.append(event(), flush=False)
        self.store.append(event(), flush=True)
        self.store.flush_pending()
        self.assertEqual(len(self.store.event_log_path.read_text(encoding="utf-8").splitlines()), 1)

    def test_corrupt_jsonl_does_not_affect_sqlite_state(self):
        self.store.append(event(), flush=True)
        with self.store.event_log_path.open("a", encoding="utf-8") as handle:
            handle.write("{broken\n")
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.read_event_log()[1], [2])

    def test_transaction_rollback_does_not_persist_event_or_outbox(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as connection:
                payload = json.dumps(event(), sort_keys=True)
                connection.execute(
                    "INSERT INTO shadow_events(event_id,event_type,root_task_id,created_at,schema_version,payload_json) VALUES(?,?,?,?,?,?)",
                    ("rollback", "test", "root", "now", "test", payload),
                )
                connection.execute(
                    "INSERT INTO jsonl_outbox(event_id,payload_json,created_at) VALUES(?,?,?)",
                    ("rollback", payload, "now"),
                )
                raise RuntimeError("rollback")
        self.assertIsNone(self.store.get("rollback"))
        self.assertIsNone(self.store.outbox_status("rollback"))


if __name__ == "__main__":
    unittest.main()
