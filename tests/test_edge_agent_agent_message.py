import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from edge_agent_agent_message import (  # noqa: E402
    AgentMessage,
    AgentMessageDedupStore,
    AgentMessageError,
    AgentMessageKeyring,
    build_message,
    deduplication_key,
    next_hop,
    verify_message,
)


class AgentMessageTests(unittest.TestCase):
    KEY = "local-test-key-with-more-than-16-bytes"

    def message(self):
        return build_message(
            session_id="sess-20260802-test",
            task_id="task-1",
            from_role="roda",
            to=("codex",),
            purpose="request_review",
            summary="독립 검토를 요청한다.",
            source_event_id="event-1",
            key_id="agent-key-v1",
            signing_key=self.KEY,
            evidence_refs=("docs/work-order.md:1",),
        )

    def test_signed_message_verifies_and_has_stable_dedupe_key(self):
        message = self.message()
        self.assertTrue(verify_message(message, self.KEY))
        self.assertEqual(deduplication_key(message), deduplication_key(message))
        self.assertNotIn(self.KEY, str(message.to_dict()))
        store = AgentMessageDedupStore()
        self.assertTrue(store.accept(message, self.KEY, expected_key_id="agent-key-v1"))
        self.assertFalse(store.accept(message, self.KEY, expected_key_id="agent-key-v1"))

    def test_tampering_or_wrong_key_fails_closed(self):
        message = self.message()
        tampered = AgentMessage(**{**message.to_dict(), "summary": "변조됨", "to": tuple(message.to)})
        with self.assertRaises(AgentMessageError):
            verify_message(tampered, self.KEY)
        with self.assertRaises(AgentMessageError):
            verify_message(message, "another-key-that-is-long-enough")

    def test_persistent_dedup_survives_new_store_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dedup.json"
            message = self.message()
            self.assertTrue(AgentMessageDedupStore(path).accept(message, self.KEY, expected_key_id="agent-key-v1"))
            self.assertFalse(AgentMessageDedupStore(path).accept(message, self.KEY, expected_key_id="agent-key-v1"))

    def test_domain_and_round_bounds_fail_closed(self):
        message = self.message()
        with self.assertRaises(AgentMessageError):
            AgentMessage(**{**message.to_dict(), "trust_domain": "external"})
        with self.assertRaises(AgentMessageError):
            build_message(
                session_id="sess-20260802-test", task_id="task-1", from_role="roda",
                to=("codex",), purpose="request_review", summary="round test",
                source_event_id="event-3", key_id="agent-key-v1", signing_key=self.KEY, round=4,
            )

    def test_keyring_rotation_keeps_previous_key_verifiable_during_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            keyring = AgentMessageKeyring(directory)
            keyring.add_key("agent-key-v1", self.KEY)
            first = self.message()
            keyring.add_key("agent-key-v2", "another-rotated-key-that-is-long-enough")
            second = build_message(
                session_id="sess-20260802-test", task_id="task-2", from_role="roda",
                to=("claude",), purpose="request_review", summary="rotated",
                source_event_id="event-4", key_id="agent-key-v2",
                signing_key=keyring.active_key()[1],
            )
            keys = keyring.verification_keys()
            self.assertTrue(verify_message(first, keys, expected_key_id="agent-key-v1"))
            self.assertTrue(verify_message(second, keys, expected_key_id="agent-key-v2"))

    def test_hop_limit_and_sensitive_content_are_blocked(self):
        message = self.message()
        forwarded = next_hop(message, recipient="codex", signing_key=self.KEY)
        self.assertEqual(forwarded.hop, 1)
        forwarded = next_hop(forwarded, recipient="antigravity", signing_key=self.KEY)
        with self.assertRaises(AgentMessageError):
            next_hop(forwarded, recipient="claude", signing_key=self.KEY)
        with self.assertRaises(AgentMessageError):
            build_message(
                session_id="sess-20260802-test", task_id="task-1", from_role="roda",
                to=("codex",), purpose="request_review", summary="token=secret",
                source_event_id="event-2", key_id="agent-key-v1", signing_key=self.KEY,
            )


if __name__ == "__main__":
    unittest.main()
