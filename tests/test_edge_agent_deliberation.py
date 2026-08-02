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
                self.assertEqual(store.snapshot(session_id)["status"], "barrier_ready")
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
        result = payload["results"]["roda"]
        self.assertTrue(result["trusted"])
        self.assertEqual(result["agent_message"]["key_id"], "agent-message-v1")

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


if __name__ == "__main__":
    unittest.main()
