import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_agent_message import build_message  # noqa: E402
from edge_agent_capability_token import CapabilityError, mint_capability, verify_capability  # noqa: E402
from edge_agent_message_bus import MessageBus, MessageBusError  # noqa: E402


KEY = "capability-key-with-more-than-16-bytes"


class CapabilityTokenTests(unittest.TestCase):
    def token(self, **overrides):
        values = {
            "subject": "codex",
            "audience": "message-bus",
            "task_id": "task-1",
            "actions": ("message.publish",),
            "key_id": "key-1",
            "signing_key": KEY,
            "now": 100.0,
            "ttl_seconds": 30.0,
            "nonce": "nonce-1",
        }
        values.update(overrides)
        return mint_capability(**values)

    def message(self, *, requires_user_report=True):
        return build_message(
            session_id="session-1",
            task_id="task-1",
            from_role="codex",
            to=("claude",),
            purpose="report",
            summary="bounded report",
            source_event_id="event-1",
            key_id="key-1",
            signing_key=KEY,
            requires_user_report=requires_user_report,
        )

    def test_scope_and_expiry_are_verified(self):
        token = self.token()
        self.assertTrue(verify_capability(
            token,
            KEY,
            required_action="message.publish",
            subject="codex",
            audience="message-bus",
            task_id="task-1",
            now=110.0,
        ))
        with self.assertRaises(CapabilityError):
            verify_capability(token, KEY, required_action="message.publish", subject="claude", audience="message-bus", task_id="task-1", now=110.0)
        with self.assertRaises(CapabilityError):
            verify_capability(token, KEY, required_action="message.publish", subject="codex", audience="message-bus", task_id="task-1", now=131.0)

    def test_tampering_and_excessive_ttl_fail_closed(self):
        token = self.token()
        forged = dict(token.to_dict(), task_id="task-2")
        with self.assertRaises(CapabilityError):
            verify_capability(forged, KEY, required_action="message.publish", subject="codex", audience="message-bus", task_id="task-1", now=110.0)
        with self.assertRaises(CapabilityError):
            self.token(ttl_seconds=3601)

    def test_report_message_requires_capability_at_bus_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            bus = MessageBus(directory)
            bus.create_session("session-1")
            with self.assertRaises(MessageBusError):
                bus.publish(self.message(), verification_key=KEY)
            published = bus.publish(self.message(), verification_key=KEY, capability=self.token(now=time.time()))
            self.assertTrue(published["message_id"])


if __name__ == "__main__":
    unittest.main()
