from pathlib import Path
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_shadow_observer import ShadowObserver, ShadowObserverConfig  # noqa: E402


def event_metadata(index: int):
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


def accounting_is_complete(stats):
    return (
        stats["submitted"]
        == stats["processed"]
        + stats["dropped_queue_full"]
        + stats["rejected_stopping"]
        + stats["abandoned_shutdown_timeout"]
        + stats["failed_store"]
        and stats["accepted"]
        == stats["processed"] + stats["abandoned_shutdown_timeout"] + stats["failed_store"]
    )


class ShadowLoadContractTests(unittest.TestCase):
    def test_sustained_input_has_no_drop_and_drains(self):
        with tempfile.TemporaryDirectory() as temp:
            observer = ShadowObserver(ShadowObserverConfig(True, Path(temp) / "shadow", queue_size=128, db_batch_size=50))
            self.assertTrue(observer.start())
            for index in range(1000):
                observer.enqueue(event_metadata(index))
                time.sleep(0.001)
            stats = observer.stop(timeout=10.0)
            self.assertTrue(accounting_is_complete(stats))
            self.assertEqual(stats["dropped_queue_full"], 0)
            self.assertEqual(stats["failed_store"], 0)
            self.assertEqual(stats["abandoned_shutdown_timeout"], 0)
            self.assertEqual(stats["worker_alive_after_stop"], 0)

    def test_burst_overload_is_bounded_and_fully_accounted(self):
        with tempfile.TemporaryDirectory() as temp:
            observer = ShadowObserver(ShadowObserverConfig(True, Path(temp) / "shadow", queue_size=32, db_batch_size=50))
            self.assertTrue(observer.start())
            for index in range(1000):
                observer.enqueue(event_metadata(index))
            stats = observer.stop(timeout=10.0)
            self.assertTrue(accounting_is_complete(stats))
            self.assertEqual(stats["failed_store"], 0)
            self.assertEqual(stats["worker_alive_after_stop"], 0)
            self.assertLessEqual(stats["queue_high_watermark"], 32)


if __name__ == "__main__":
    unittest.main()
