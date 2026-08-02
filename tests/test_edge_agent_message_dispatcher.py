import multiprocessing
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from edge_agent_agent_message import build_message  # noqa: E402
from edge_agent_message_bus import MessageBus  # noqa: E402
from edge_agent_message_dispatcher import DispatchOutcome, MessageDispatcher  # noqa: E402


KEY = b"dispatcher-test-key-with-more-than-16-bytes"


class MessageDispatcherTests(unittest.TestCase):
    def _message(self):
        return build_message(
            session_id="session-dispatch",
            task_id="task-dispatch",
            from_role="codex",
            to=("claude",),
            purpose="follow_up",
            summary="검토해줘",
            source_event_id="dispatch-event-1",
            key_id="test-key",
            signing_key=KEY,
        )

    def test_handler_completion_is_checkpointed_and_acked(self):
        with tempfile.TemporaryDirectory() as directory:
            bus = MessageBus(directory)
            bus.create_session("session-dispatch")
            item = bus.publish(self._message(), verification_key=KEY)
            result = MessageDispatcher(bus).dispatch_once(
                "claude",
                lambda message: DispatchOutcome(summary=f"handled:{message.purpose}"),
                owner="claude-worker",
            )
            self.assertEqual(result, {"claimed": 1, "completed": 1, "requeued": 0, "failed": 0})
            self.assertEqual(bus.claim("claude", owner="claude-worker"), [])
            self.assertEqual(bus.recoverable("session-dispatch"), [])
            self.assertEqual(bus.transcript("session-dispatch")[0].purpose, "follow_up")

    def test_handler_failure_requeues_then_eventually_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            bus = MessageBus(directory)
            bus.create_session("session-dispatch")
            bus.publish(self._message(), verification_key=KEY)
            dispatcher = MessageDispatcher(bus)
            for _ in range(2):
                result = dispatcher.dispatch_once("claude", lambda _: (_ for _ in ()).throw(RuntimeError("provider down")), owner="worker")
                self.assertEqual(result["requeued"], 1)
            result = dispatcher.dispatch_once("claude", lambda _: (_ for _ in ()).throw(RuntimeError("provider down")), owner="worker")
            self.assertEqual(result["failed"], 1)
            self.assertEqual(bus.claim("claude", owner="worker"), [])

    def test_sigkill_recovery_reclaims_expired_lease_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            bus = MessageBus(directory)
            bus.create_session("session-dispatch")
            bus.publish(self._message(), verification_key=KEY)

            def crash_worker(root):
                child_bus = MessageBus(root)
                claims = child_bus.claim("claude", owner="crash-worker", lease_seconds=1)
                child_bus.checkpoint("session-dispatch", claims[0]["message"]["task_id"], "dispatch", "claimed")
                os.kill(os.getpid(), signal.SIGKILL)

            context = multiprocessing.get_context("fork")
            process = context.Process(target=crash_worker, args=(directory,))
            process.start()
            process.join(5)
            self.assertEqual(process.exitcode, -signal.SIGKILL)
            self.assertTrue(bus.recoverable("session-dispatch"))
            time.sleep(1.1)
            result = MessageDispatcher(bus).dispatch_once(
                "claude",
                lambda message: DispatchOutcome(summary="recovered after restart"),
                owner="recovery-worker",
            )
            self.assertEqual(result["completed"], 1)
            self.assertEqual(bus.recoverable("session-dispatch"), [])


if __name__ == "__main__":
    unittest.main()
