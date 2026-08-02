import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from edge_agent_agent_message import build_message  # noqa: E402
from edge_agent_message_bus import MessageBus, MessageBusError, delegate_message  # noqa: E402


KEY = b"local-message-bus-test-key-2026"


class MessageBusTests(unittest.TestCase):
    def make_message(self, *, source="event-1", round_number=1):
        return build_message(
            session_id="session-1",
            task_id="root-task",
            from_role="codex",
            to=("claude", "antigravity"),
            purpose="peer_review",
            summary="검토 결과",
            source_event_id=source,
            key_id="test-key",
            signing_key=KEY,
            round=round_number,
        )

    def test_publish_claim_ack_survives_new_bus_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            first = MessageBus(directory)
            first.create_session("session-1", "논의")
            item = first.publish(self.make_message(), verification_key=KEY)
            second = MessageBus(directory)
            claimed = second.claim("claude", session_id="session-1", owner="claude-worker")
            self.assertEqual(claimed[0]["message_id"], item["message_id"])
            self.assertTrue(second.acknowledge("session-1", item["message_id"], owner="claude-worker"))
            self.assertEqual(len(second.claim("claude", session_id="session-1", owner="claude-worker")), 0)
            self.assertEqual(len(second.claim("antigravity", session_id="session-1", owner="antigravity-worker")), 1)

    def test_duplicate_source_event_is_not_delivered_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            bus = MessageBus(directory)
            bus.create_session("session-1")
            message = self.make_message()
            bus.publish(message, verification_key=KEY)
            bus.publish(message, verification_key=KEY)
            self.assertEqual(len(bus.transcript("session-1")), 1)

    def test_task_graph_blocks_until_dependency_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            bus = MessageBus(directory)
            bus.create_session("session-1")
            bus.spawn_task("session-1", "research", owner="codex", purpose="조사")
            bus.spawn_task("session-1", "synthesis", owner="claude", purpose="통합", parent_task_id="research", depends_on=("research",))
            self.assertEqual([item["task_id"] for item in bus.ready_tasks("session-1")], ["research"])
            bus.update_task("session-1", "research", "completed", summary="완료")
            self.assertEqual([item["task_id"] for item in bus.ready_tasks("session-1")], ["synthesis"])

    def test_delegation_is_bounded(self):
        parent = self.make_message()
        child = delegate_message(
            parent,
            to_role="claude",
            purpose="follow_up",
            summary="추가 검토",
            source_event_id="event-2",
            key_id="test-key",
            signing_key=KEY,
        )
        self.assertEqual(child.hop, 1)
        self.assertEqual(child.round, 2)
        self.assertEqual(child.to, ("claude",))

        grandchild = delegate_message(
            child,
            to_role="roda",
            purpose="follow_up",
            summary="추가 검토",
            source_event_id="event-3",
            key_id="test-key",
            signing_key=KEY,
        )
        with self.assertRaises(MessageBusError):
            delegate_message(
                grandchild,
                to_role="roda",
                purpose="follow_up",
                summary="한도 초과",
                source_event_id="event-4",
                key_id="test-key",
                signing_key=KEY,
            )


if __name__ == "__main__":
    unittest.main()
