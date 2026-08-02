import tempfile
import unittest
from unittest.mock import patch

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from edge_agent_deliberation import DeliberationStore, session_id_for_telegram  # noqa: E402


class DeliberationStoreTests(unittest.TestCase):
    def test_barrier_is_durable_and_ready_only_after_all_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                session_id = session_id_for_telegram("-1", 42)
                store.start(session_id, "논의 요청")
                for role in ("claude", "codex", "antigravity"):
                    store.record(session_id, role, status="completed", summary=f"{role} 의견")
                self.assertEqual(store.snapshot(session_id)["status"], "collecting")
                store.record(session_id, "roda", status="completed", summary="roda 의견")
                self.assertEqual(store.snapshot(session_id)["status"], "collecting")
                self.assertEqual(store.round_state(session_id, 1), "ready")
                self.assertIn("codex 의견", store.render(session_id))

    def test_same_telegram_message_has_same_session_id(self):
        self.assertEqual(session_id_for_telegram("-1", 7), session_id_for_telegram("-1", 7))
        self.assertNotEqual(session_id_for_telegram("-1", 7), session_id_for_telegram("-1", 8))

    def test_runtime_result_is_signed_when_key_file_is_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                session_id = session_id_for_telegram("-1", 99)
                store.start(session_id, "논의 요청")
                store.record(session_id, "roda", status="completed", summary="실행 의견")
                payload = store.snapshot(session_id)
                transcript = store._bus.transcript(session_id)
        result = payload["results"]["roda"]
        self.assertTrue(result["trusted"])
        self.assertIn(f"{session_id}-roda-round-1", result["evidence_refs"])
        self.assertEqual(result["agent_message"]["key_id"], "agent-message-v1")
        self.assertIn(f"{session_id}-roda-round-1", result["agent_message"]["evidence_refs"])
        self.assertEqual(transcript[0].from_role, "roda")
        self.assertIn("claude", transcript[0].to)

    def test_render_records_recipient_delivery_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                session_id = session_id_for_telegram("-1", 103)
                store.start(session_id, "전달 증명")
                store.record(session_id, "roda", status="completed", summary="peer 결과")
                self.assertIn("bus_delivery=not_requested", store.render(session_id))
                rendered = store.render(session_id, consumer_role="claude")
                self.assertIn("bus_delivery=role=claude;claimed=1;acked=1", rendered)
                item = store._bus._read(session_id)["messages"][0]
                self.assertEqual(item["deliveries"]["claude"]["status"], "acked")

    def test_gemma_legacy_role_alias_is_normalized_to_roda(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                session_id = session_id_for_telegram("-1", 100)
                store.start(session_id, "논의 요청")
                store.record(session_id, "gemma", status="completed", summary="legacy alias")
                self.assertEqual(store.snapshot(session_id)["results"]["roda"]["summary"], "legacy alias")

    def test_three_round_peer_adjudication_is_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                session_id = session_id_for_telegram("-1", 101)
                store.start(session_id, "4개 역할이 논의")
                for role in ("claude", "codex", "antigravity", "roda"):
                    store.record(session_id, role, status="completed", summary=f"{role} 1차")
                self.assertEqual(len(store._bus.transcript(session_id)), 4)
                self.assertEqual(store.wait_for_round(session_id, 1, timeout_seconds=0)["status"], "collecting")
                self.assertEqual(store.round_state(session_id, 1), "ready")
                for role in ("claude", "codex", "antigravity", "roda"):
                    store.record(session_id, role, status="completed", summary=f"{role} 2차", round_number=2)
                transcript = store._bus.transcript(session_id)
                self.assertEqual(len(transcript), 8)
                self.assertTrue(any(message.round == 2 for message in transcript))
                self.assertIn("round=2", store.render(session_id))
                for role in ("claude", "codex", "antigravity", "roda"):
                    store.record(session_id, role, status="completed", summary=f"{role} 최종", round_number=3)
                transcript = store._bus.transcript(session_id)
                self.assertEqual(len(transcript), 12)
                self.assertTrue(any(message.round == 3 for message in transcript))
                self.assertIn("round=3", store.render(session_id))
                self.assertEqual(store.snapshot(session_id)["status"], "barrier_ready")

    def test_failed_round_blocks_final_barrier(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                session_id = session_id_for_telegram("-1", 102)
                store.start(session_id, "실패 전파")
                for role in ("claude", "codex", "antigravity"):
                    store.record(session_id, role, status="completed", summary=f"{role} 1차")
                store.record(session_id, "roda", status="completed", summary="roda 1차")
                store.record(session_id, "claude", status="failed", summary="provider usage limit", round_number=2)
                self.assertEqual(store.round_state(session_id, 2), "failed")
                self.assertNotEqual(store.snapshot(session_id)["status"], "barrier_ready")


if __name__ == "__main__":
    unittest.main()
