import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from edge_agent_deliberation import (  # noqa: E402
    DeliberationStore,
    configured_conversation_timeout_seconds,
    first_pass_prompt,
    session_id_for_telegram,
    should_publish_failure,
    should_publish_user_result,
)

BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
_BOT_TOKEN_ROOT = tempfile.TemporaryDirectory()
_BOT_TOKEN_FILE = Path(_BOT_TOKEN_ROOT.name) / "claude.token"
_BOT_TOKEN_FILE.write_text("123456:unit-test-token", encoding="utf-8")
_BOT_TOKEN_FILE.chmod(0o600)
_BOT_RUNTIME_HOME_ROOT = tempfile.TemporaryDirectory()
os.environ.setdefault("TELEGRAM_AGENT_ROLE", "claude")
os.environ.setdefault("TELEGRAM_AGENT_CHAT_ID", "-1003952617795")
os.environ.setdefault("TELEGRAM_AGENT_TOKEN_FILE", str(_BOT_TOKEN_FILE))
_BOT_SPEC = importlib.util.spec_from_file_location(
    "telegram_agent_bot_for_deliberation_tests", BIN_DIR / "telegram-agent-bot.py"
)
BOT_MODULE = importlib.util.module_from_spec(_BOT_SPEC)
assert _BOT_SPEC and _BOT_SPEC.loader
with patch.object(Path, "home", return_value=Path(_BOT_RUNTIME_HOME_ROOT.name)):
    _BOT_SPEC.loader.exec_module(BOT_MODULE)


class DeliberationStoreTests(unittest.TestCase):
    def test_conversation_timeout_is_short_and_bounded(self):
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("EDGE_AGENT_CONVERSATION_MEETING_TIMEOUT_SECONDS", None)
            self.assertEqual(configured_conversation_timeout_seconds(), 60.0)
        with patch.dict("os.environ", {"EDGE_AGENT_CONVERSATION_MEETING_TIMEOUT_SECONDS": "999"}):
            self.assertEqual(configured_conversation_timeout_seconds(), 120.0)

    def test_only_coordinator_publishes_a_deliberation_result(self):
        self.assertTrue(should_publish_user_result("claude", "delib-1"))
        self.assertFalse(should_publish_user_result("codex", "delib-1"))
        self.assertFalse(should_publish_user_result("antigravity", "delib-1"))
        self.assertFalse(should_publish_user_result("roda", "delib-1"))
        self.assertTrue(should_publish_user_result("codex", None))
        self.assertFalse(should_publish_user_result("claude", "delib-1", conversation_meeting=True))
        self.assertTrue(should_publish_user_result("codex", "delib-1", conversation_meeting=True))

    def test_only_deputy_can_publish_a_meeting_failure(self):
        self.assertFalse(should_publish_failure("claude", "delib-1"))
        self.assertFalse(should_publish_failure("antigravity", "delib-1"))
        self.assertFalse(should_publish_failure("roda", "delib-1"))
        self.assertTrue(should_publish_failure("codex", "delib-1"))
        self.assertTrue(should_publish_failure("claude", None))

    def test_first_pass_cannot_impersonate_peer_roles(self):
        prompt = first_pass_prompt("antigravity", "현재 시스템 장단점을 회의해줘")
        self.assertIn("역할=antigravity", prompt)
        self.assertIn("다른 에이전트의 의견을 대신 작성", prompt)
        self.assertIn("내부 패킷", prompt)
        self.assertNotIn("Claude 관점:", prompt)
        self.assertIn("Git, worktree, 테스트", prompt)

    def test_conversation_meeting_uses_one_round_and_tolerates_failed_participant(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                session_id = session_id_for_telegram("-1", 106)
                payload = store.start(session_id, "장단점을 회의해줘", mode="conversation")
                self.assertEqual(payload["max_rounds"], 1)
                for role in ("claude", "codex", "antigravity"):
                    store.record(session_id, role, status="completed", summary=f"{role} 의견")
                store.record(session_id, "roda", status="failed", summary="provider unavailable")
                self.assertEqual(store.round_state(session_id, 1), "ready")
                self.assertEqual(store.snapshot(session_id)["status"], "barrier_ready")
                rendered = store.render_conversation(session_id)
                self.assertIn("Claude: claude 의견", rendered)
                self.assertIn("Roda", rendered)
                self.assertNotIn("provider unavailable", rendered)
                self.assertNotIn("message bus", rendered)

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

    def test_append_human_note_is_ordered_and_idempotent_by_message_id(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                session_id = session_id_for_telegram("-1", 200)
                store.start(session_id, "회의 시작")
                store.append_human_note(session_id, "첫 발언", telegram_message_id=201)
                store.append_human_note(session_id, "둘째 발언", telegram_message_id=202)
                notes = store.snapshot(session_id)["human_notes"]
                self.assertEqual([note["seq"] for note in notes], [1, 2])
                self.assertEqual(notes[0]["text"], "첫 발언")
                self.assertEqual(notes[0]["telegram_message_id"], "201")
                store.append_human_note(session_id, "첫 발언 재전송", telegram_message_id=201)
                self.assertEqual(len(store.snapshot(session_id)["human_notes"]), 2)

    def test_append_human_note_rejects_unknown_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeliberationStore(directory)
            with self.assertRaises(ValueError):
                store.append_human_note("delib-missing", "발언", telegram_message_id=1)

    def test_record_stores_observed_human_seq_and_defaults_to_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                session_id = session_id_for_telegram("-1", 210)
                store.start(session_id, "회의")
                store.append_human_note(session_id, "발언1", telegram_message_id=1)
                self.assertEqual(store.latest_human_seq(session_id), 1)
                store.record(session_id, "roda", status="completed", summary="roda 의견", observed_human_seq=store.latest_human_seq(session_id))
                self.assertEqual(store.snapshot(session_id)["results"]["roda"]["observed_human_seq"], 1)
                store.record(session_id, "codex", status="completed", summary="codex 의견")
                self.assertEqual(store.snapshot(session_id)["results"]["codex"]["observed_human_seq"], 0)
                self.assertEqual(store.latest_human_seq("delib-does-not-exist"), 0)

    def test_active_session_for_chat_tracks_pointer_and_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                chat_id = -1003952617795
                session_id = session_id_for_telegram(chat_id, 220)
                self.assertIsNone(store.active_session_for_chat(chat_id))
                store.start(session_id, "회의")
                store.record_active_chat_session(chat_id, session_id)
                self.assertEqual(store.active_session_for_chat(chat_id), session_id)
                store.close_human_notes(session_id)
                self.assertIsNone(store.active_session_for_chat(chat_id))

    def test_active_session_for_chat_is_none_once_barrier_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                chat_id = -1003952617795
                session_id = session_id_for_telegram(chat_id, 221)
                store.start(session_id, "회의", mode="conversation")
                store.record_active_chat_session(chat_id, session_id)
                self.assertEqual(store.active_session_for_chat(chat_id), session_id)
                for role in ("claude", "codex", "antigravity", "roda"):
                    store.record(session_id, role, status="completed", summary=f"{role} 의견")
                self.assertEqual(store.snapshot(session_id)["status"], "barrier_ready")
                self.assertIsNone(store.active_session_for_chat(chat_id))

    def test_render_includes_unreflected_human_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                session_id = session_id_for_telegram("-1", 211)
                store.start(session_id, "회의")
                store.append_human_note(session_id, "중간에 끼어든 발언", telegram_message_id=5)
                rendered = store.render(session_id)
                self.assertIn("seq=1", rendered)
                self.assertIn("중간에 끼어든 발언", rendered)

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

    def test_failed_claude_is_visible_for_codex_coordinator_takeover(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                session_id = session_id_for_telegram("-1", 105)
                store.start(session_id, "fallback test")
                store.record(session_id, "claude", status="failed", summary="provider failure")
                store.record(session_id, "codex", status="completed", summary="codex evidence")
                self.assertTrue(store.coordinator_failed(session_id, 1))


    def test_barrier_timeout_message_lists_incomplete_roles_without_claiming_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                with patch.object(BOT_MODULE, "DeliberationStore", lambda *a, **k: DeliberationStore(directory)):
                    store = DeliberationStore(directory)
                    session_id = session_id_for_telegram("-1", 104)
                    store.start(session_id, "3차 barrier 타임아웃")
                    for role in ("claude", "codex", "antigravity"):
                        store.record(session_id, role, status="completed", summary=f"{role} 1차")
                    store.record(session_id, "roda", status="completed", summary="roda 1차")
                    for role in ("claude", "codex", "antigravity", "roda"):
                        store.record(session_id, role, status="completed", summary=f"{role} 2차", round_number=2)
                    # codex only reaches round 3; antigravity and roda never complete it.
                    store.record(session_id, "claude", status="completed", summary="claude 최종", round_number=3)
                    store.record(session_id, "codex", status="failed", summary="usage limit", round_number=3)

                    with self.assertRaises(RuntimeError):
                        BOT_MODULE._require_deliberation_round(session_id, 3, timeout_seconds=0)

                    message = BOT_MODULE._deliberation_barrier_timeout_message(
                        session_id, 3, RuntimeError("deliberation round 3 barrier timeout")
                    )
                    self.assertIn("antigravity", message)
                    self.assertIn("roda", message)
                    self.assertIn("codex", message)
                    self.assertIn("완료한 것으로 처리하지 않았습니다", message)


if __name__ == "__main__":
    unittest.main()
