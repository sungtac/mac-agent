import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_agent_message import AgentMessageError, build_message, verify_message  # noqa: E402
from edge_agent_trace import TraceContext, event_trace  # noqa: E402


class TracePropagationTests(unittest.TestCase):
    KEY = "0123456789abcdef0123456789abcdef"

    def test_peer_message_carries_trace_and_forwarded_span(self):
        message = build_message(
            session_id="session-1",
            task_id="task-1",
            from_role="codex",
            to=("claude",),
            purpose="peer-review",
            summary="review",
            source_event_id="event-1",
            key_id="key-1",
            signing_key=self.KEY,
        )
        verify_message(message, self.KEY)
        forwarded = replace(
            message,
            from_role="claude",
            to=("claude",),
            parent_span_id=message.span_id,
            span_id="",
            signature="",
        )
        self.assertEqual(forwarded.trace_id, message.trace_id)
        self.assertNotEqual(forwarded.span_id, message.span_id)
        self.assertEqual(forwarded.parent_span_id, message.span_id)

    def test_trace_tampering_invalidates_signed_message(self):
        message = build_message(
            session_id="session-1",
            task_id="task-1",
            from_role="codex",
            to=("claude",),
            purpose="peer-review",
            summary="review",
            source_event_id="event-1",
            key_id="key-1",
            signing_key=self.KEY,
        )
        forged = replace(message, trace_id="trace-forged")
        with self.assertRaises(AgentMessageError):
            verify_message(forged, self.KEY)

    def test_durable_event_trace_round_trip(self):
        context = event_trace("session-1", "task-1")
        restored = TraceContext.from_dict(context.to_dict())
        self.assertEqual(restored.to_dict(), context.to_dict())
        self.assertTrue(restored.trace_id.startswith("trace-"))
        self.assertTrue(restored.span_id.startswith("span-"))


if __name__ == "__main__":
    unittest.main()
