import tempfile
import time
import unittest
import multiprocessing
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from edge_agent_egress_queue import EgressQueueError, SharedEgressQueue  # noqa: E402


class SharedEgressQueueTests(unittest.TestCase):
    @staticmethod
    def _worker(root: str, result_queue) -> None:
        queue = SharedEgressQueue(root, interval_seconds=0.01, wait_seconds=2)
        with queue.acquire("chat", delivery_id="multi", chunk_index=0) as permit:
            result_queue.put(permit.sequence)

    def test_serializes_and_records_order(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = SharedEgressQueue(directory, interval_seconds=0.01, wait_seconds=1)
            with queue.acquire("chat", delivery_id="delivery-1", chunk_index=0) as first:
                self.assertEqual(first.sequence, 1)
            with queue.acquire("chat", delivery_id="delivery-1", chunk_index=1) as second:
                self.assertEqual(second.sequence, 2)
            state = Path(directory, "queue.json").read_text(encoding="utf-8")
            self.assertIn('"last_chunk_index":1', state)

    def test_bounded_backpressure_fails_without_clobbering_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = SharedEgressQueue(directory, interval_seconds=0.01, wait_seconds=0.1)
            permit = queue.acquire("chat")
            try:
                blocked = SharedEgressQueue(directory, interval_seconds=0.01, wait_seconds=0.1)
                started = time.monotonic()
                with self.assertRaises(EgressQueueError):
                    blocked.acquire("chat", wait_seconds=0.1)
                self.assertLess(time.monotonic() - started, 0.5)
            finally:
                permit.release()

    def test_different_chats_do_not_share_the_send_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = SharedEgressQueue(directory, interval_seconds=0.01, wait_seconds=0.2)
            first = queue.acquire("chat-a")
            try:
                started = time.monotonic()
                with queue.acquire("chat-b"):
                    pass
                self.assertLess(time.monotonic() - started, 0.15)
            finally:
                first.release()

    def test_two_processes_share_one_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("fork")
            result_queue = context.Queue()
            processes = [context.Process(target=self._worker, args=(directory, result_queue)) for _ in range(2)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(5)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual({result_queue.get(timeout=1), result_queue.get(timeout=1)}, {1, 2})


if __name__ == "__main__":
    unittest.main()
