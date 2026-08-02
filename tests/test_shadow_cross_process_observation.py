from pathlib import Path
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_claim_store import ClaimStore, CLAIMED  # noqa: E402
from edge_agent_shadow_event_store import ShadowEventStore, ShadowEventStoreBusy  # noqa: E402
from edge_agent_shadow_observer import build_shadow_events  # noqa: E402


def metadata(bot_id):
    return {
        "root_task_id": "root-group-1",
        "revision_id": "revision-1",
        "cross_bot_message_key": "message-key",
        "telegram_update_key": f"update-{bot_id}",
        "bot_id": bot_id,
        "bot_role": bot_id,
        "message_length": 3,
        "attachment_count": 0,
        "attachment_metadata": [],
        "body_hash": None,
        "body_hash_status": "UNKNOWN",
        "task_type": "UNKNOWN",
        "risk_level": "UNKNOWN",
        "observed_provider_role": bot_id,
    }


class ShadowCrossProcessTests(unittest.TestCase):
    def test_logical_event_is_shared_but_observation_event_is_bot_specific(self):
        first = build_shadow_events(metadata("claude"))
        second = build_shadow_events(metadata("codex"))
        self.assertEqual(first[0]["event_id"], second[0]["event_id"])
        self.assertNotEqual(first[1]["event_id"], second[1]["event_id"])

    def test_two_store_instances_idempotently_store_same_logical_event(self):
        with tempfile.TemporaryDirectory() as temp:
            first = ShadowEventStore(Path(temp) / "shadow")
            second = ShadowEventStore(Path(temp) / "shadow")
            event = build_shadow_events(metadata("claude"))[0]
            results = []

            def write(store):
                results.append(store.append(event, flush=False))

            threads = [threading.Thread(target=write, args=(store,)) for store in (first, second)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(first.count(), 1)
            self.assertEqual(sum(1 for result in results if result["inserted"]), 1)

    def test_sqlite_busy_is_explicit_and_does_not_replace_state(self):
        with tempfile.TemporaryDirectory() as temp:
            locked = ShadowEventStore(Path(temp) / "shadow", busy_timeout_ms=1)
            contender = ShadowEventStore(Path(temp) / "shadow", busy_timeout_ms=1)
            with locked.transaction() as connection:
                connection.execute("INSERT INTO shadow_meta(key,value) VALUES('held','1')")
                with self.assertRaises(ShadowEventStoreBusy):
                    contender.append(build_shadow_events(metadata("codex"))[0], flush=False)
            self.assertEqual(contender.count(), 0)

    def test_previous_fencing_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            clock = [100.0]
            store = ClaimStore(Path(temp) / "claims.db", clock=lambda: clock[0], ttl_seconds=10)
            first = store.claim("message", "root", "owner-a")
            clock[0] = 111.0
            second = store.claim("message", "root", "owner-b")
            self.assertEqual(second.status, CLAIMED)
            self.assertFalse(store.validate_fencing_token("message", first.fencing_token))


if __name__ == "__main__":
    unittest.main()
