import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_shadow_event_store import (  # noqa: E402
    EventConflictError,
    SensitiveDataError,
    ShadowEventStore,
    ShadowEventStoreBusy,
)


class ShadowEventStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = ShadowEventStore(Path(self.tempdir.name) / "shadow")

    def tearDown(self):
        self.tempdir.cleanup()

    def event(self, **extra):
        value = {
            "event_id": "event-1",
            "event_type": "ingress_observed",
            "root_task_id": "root-1",
            "message_identity_hash": "message-hash",
            "body_hash": "body-hash",
            "attachment_hashes": [],
            "classification": {"task_type": "research", "risk_level": "LOW"},
            "claim_result": {"status": "CLAIMED"},
        }
        value.update(extra)
        return value

    def test_same_event_is_idempotent_and_jsonl_is_append_only(self):
        first = self.store.append(self.event())
        second = self.store.append(self.event())
        self.assertTrue(first["inserted"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(len(self.store.event_log_path.read_text(encoding="utf-8").splitlines()), 1)

    def test_event_id_conflict_is_rejected(self):
        self.store.append(self.event())
        with self.assertRaises(EventConflictError):
            self.store.append(self.event(event_type="different"))

    def test_sensitive_raw_content_is_rejected(self):
        with self.assertRaises(SensitiveDataError):
            self.store.append(self.event(message_text="do not persist"))
        with self.assertRaises(SensitiveDataError):
            self.store.append(self.event(token="secret"))

    def test_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT INTO shadow_events(event_id,event_type,root_task_id,created_at,schema_version,payload_json) VALUES(?,?,?,?,?,?)",
                    ("rollback", "test", "root", "now", "test", "{}"),
                )
                raise RuntimeError("rollback")
        self.assertIsNone(self.store.get("rollback"))

    def test_corrupt_jsonl_line_does_not_break_sqlite_state(self):
        self.store.append(self.event())
        with self.store.event_log_path.open("a", encoding="utf-8") as handle:
            handle.write("{not-json}\n")
        events, corrupt = self.store.read_event_log()
        self.assertEqual(len(events), 1)
        self.assertEqual(corrupt, [2])
        self.assertEqual(len(self.store.list_events("root-1")), 1)

    def test_later_revision_does_not_overwrite_completed_event(self):
        self.store.append(self.event(event_id="completed", revision_id="revision-1", claim_result={"status": "COMPLETED"}))
        self.store.append(self.event(event_id="edited", revision_id="revision-2", claim_result={"status": "PENDING"}))
        self.assertEqual(self.store.get("completed")["claim_result"]["status"], "COMPLETED")
        self.assertEqual(len(self.store.list_events("root-1")), 2)

    def test_shadow_store_uses_only_injected_path(self):
        self.assertTrue(self.store.database_path.is_relative_to(Path(self.tempdir.name)))
        self.assertNotEqual(self.store.root, Path.home() / ".edge-agent" / "state")
        manifest = self.store.manifest_path.read_text(encoding="utf-8")
        self.assertIn("stores_raw_content: false", manifest)
        self.assertIn("sqlite_journal_mode: WAL", manifest)

    def test_sqlite_busy_is_reported(self):
        locked = ShadowEventStore(Path(self.tempdir.name) / "busy", busy_timeout_ms=1)
        contender = ShadowEventStore(Path(self.tempdir.name) / "busy", busy_timeout_ms=1)
        with locked.transaction() as connection:
            connection.execute("INSERT INTO shadow_meta(key,value) VALUES('held','1')")
            with self.assertRaises(ShadowEventStoreBusy):
                contender.append(self.event(event_id="busy-event"))


if __name__ == "__main__":
    unittest.main()
