from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_shadow_event_store import ShadowEventStore, ShadowEventStoreBusy  # noqa: E402
from edge_agent_shadow_observer import (  # noqa: E402
    DRAINING,
    NEW,
    RUNNING,
    STOPPED,
    STOPPING,
    ShadowObserver,
    ShadowObserverConfig,
    build_shadow_events,
)


def metadata(index: int) -> dict[str, object]:
    return {
        "root_task_id": f"root-{index}",
        "revision_id": f"revision-{index}",
        "cross_bot_message_key": f"message-{index}",
        "telegram_update_key": f"update-{index}",
        "bot_id": "codex",
        "bot_role": "codex",
        "message_length": 0,
        "attachment_count": 0,
        "attachment_metadata": [],
        "body_hash": None,
        "body_hash_status": "UNKNOWN",
        "task_type": "UNKNOWN",
        "risk_level": "UNKNOWN",
        "observed_provider_role": "codex",
    }


class ShadowLifecycleBatchTests(unittest.TestCase):
    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            threading.Event().wait(0.01)
        return predicate()

    def test_lifecycle_states_and_idempotent_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            observer = ShadowObserver(ShadowObserverConfig(True, Path(temp) / "shadow"))
            self.assertEqual(observer.state, NEW)
            self.assertTrue(observer.start())
            self.assertEqual(observer.state, RUNNING)
            observer.stop(timeout=1.0)
            self.assertEqual(observer.state, STOPPED)
            self.assertEqual(observer.stats["worker_alive_after_stop"], 0)
            observer.stop(timeout=0.0)
            self.assertEqual(observer.state, STOPPED)

    def test_stop_closes_worker_before_root_can_be_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "shadow"
            observer = ShadowObserver(ShadowObserverConfig(True, root, queue_size=32, db_batch_size=10))
            self.assertTrue(observer.start())
            for index in range(200):
                observer.enqueue(metadata(index))
            result = observer.stop(timeout=0.05)
            self.assertEqual(result["worker_alive_after_stop"], 0)
            self.assertEqual(observer.state, STOPPED)
            self.assertEqual(result["queue_remaining_at_stop"], 0)
            self.assertEqual(observer.store.active_connection_count, 0)
            # No worker can reopen files after this point.
            import shutil
            shutil.rmtree(root)
            self.assertFalse(root.exists())

    def test_submission_accounting_is_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            observer = ShadowObserver(ShadowObserverConfig(True, Path(temp) / "shadow", queue_size=1))
            self.assertTrue(observer.start())
            for index in range(100):
                observer.enqueue(metadata(index))
            observer.stop(timeout=0.01)
            stats = observer.stats
            self.assertEqual(
                stats["submitted"],
                stats["processed"]
                + stats["dropped_queue_full"]
                + stats["rejected_stopping"]
                + stats["abandoned_shutdown_timeout"]
                + stats["failed_store"],
            )
            self.assertEqual(stats["accepted"], stats["processed"] + stats["abandoned_shutdown_timeout"] + stats["failed_store"])
            self.assertEqual(stats["worker_alive_after_stop"], 0)

    def test_events_after_stopping_are_rejected_and_accounted(self):
        with tempfile.TemporaryDirectory() as temp:
            observer = ShadowObserver(ShadowObserverConfig(True, Path(temp) / "shadow"))
            self.assertTrue(observer.start())
            observer.stop(timeout=1.0)
            self.assertFalse(observer.enqueue(metadata(1)))
            stats = observer.stats
            self.assertEqual(stats["rejected_stopping"], 1)
            self.assertEqual(stats["submitted"], 1)

    def test_batch_writer_uses_configured_batch_size(self):
        with tempfile.TemporaryDirectory() as temp:
            observer = ShadowObserver(ShadowObserverConfig(True, Path(temp) / "shadow", db_batch_size=10, db_batch_max_wait_seconds=0.001))
            self.assertTrue(observer.start())
            calls = []
            original = observer.store.append_batch

            def wrapped(events, **kwargs):
                calls.append(len(events))
                return original(events, **kwargs)

            observer.store.append_batch = wrapped
            for index in range(25):
                observer.enqueue(metadata(index))
            self.assertTrue(self.wait_for(lambda: observer.stats["processed"] >= 25))
            observer.stop(timeout=2.0)
            self.assertTrue(calls)
            self.assertLess(len(calls), 25)
            self.assertLessEqual(max(calls), 20)  # 10 metadata items, two events each

    def test_store_batch_keeps_event_and_outbox_transactional(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ShadowEventStore(Path(temp) / "shadow")
            events = build_shadow_events(metadata(1)) + build_shadow_events(metadata(2))
            result = store.append_batch(events, flush=False)
            self.assertEqual(len(result["results"]), 4)
            self.assertEqual(store.count(), 4)
            self.assertEqual(store.pending_outbox_count(), 4)

    def test_store_batch_conflict_does_not_discard_unrelated_events(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ShadowEventStore(Path(temp) / "shadow")
            first = build_shadow_events(metadata(1))[0]
            store.append(first, flush=False)
            conflicting = dict(first)
            conflicting["message_identity_hash"] = "different"
            unrelated = build_shadow_events(metadata(2))[0]
            result = store.append_batch([conflicting, unrelated], flush=False)
            self.assertEqual([item["status"] for item in result["results"]], ["conflict", "inserted"])
            self.assertEqual(store.count(), 2)

    def test_failed_store_is_accounted_without_worker_leak(self):
        with tempfile.TemporaryDirectory() as temp:
            observer = ShadowObserver(ShadowObserverConfig(True, Path(temp) / "shadow"))
            self.assertTrue(observer.start())
            observer.store.append_batch = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("simulated disk full"))
            observer.enqueue(metadata(1))
            self.assertTrue(self.wait_for(lambda: observer.stats["failed_store"] == 1))
            observer.stop(timeout=1.0)
            self.assertEqual(observer.stats["worker_alive_after_stop"], 0)
            self.assertEqual(observer.stats["submitted"], observer.stats["failed_store"])

    def test_jsonl_failure_does_not_reclassify_committed_sqlite_event(self):
        with tempfile.TemporaryDirectory() as temp:
            observer = ShadowObserver(ShadowObserverConfig(True, Path(temp) / "shadow"))
            self.assertTrue(observer.start())
            observer.store.flush_pending = lambda **kwargs: (_ for _ in ()).throw(OSError("jsonl unavailable"))
            observer.enqueue(metadata(1))
            self.assertTrue(self.wait_for(lambda: observer.stats["processed"] == 1))
            observer.stop(timeout=1.0)
            self.assertEqual(observer.stats["failed_store"], 0)
            self.assertGreaterEqual(observer.stats["jsonl_errors"], 1)
            self.assertEqual(observer.store.count(), 2)

    def test_sqlite_busy_is_failed_store_without_worker_leak(self):
        with tempfile.TemporaryDirectory() as temp:
            observer = ShadowObserver(ShadowObserverConfig(True, Path(temp) / "shadow"))
            self.assertTrue(observer.start())
            observer.store.append_batch = lambda *args, **kwargs: (_ for _ in ()).throw(ShadowEventStoreBusy("busy"))
            observer.enqueue(metadata(1))
            self.assertTrue(self.wait_for(lambda: observer.stats["failed_store"] == 1))
            observer.stop(timeout=1.0)
            self.assertEqual(observer.stats["worker_alive_after_stop"], 0)

    def test_start_failure_then_stop_is_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            observer = ShadowObserver(ShadowObserverConfig(True, Path("/dev/null/impossible")))
            self.assertFalse(observer.start())
            self.assertEqual(observer.state, "FAILED")
            observer.stop()
            self.assertEqual(observer.state, "FAILED")


if __name__ == "__main__":
    unittest.main()
