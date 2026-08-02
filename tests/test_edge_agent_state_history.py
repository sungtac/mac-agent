import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import edge_agent_state as state  # noqa: E402


class TaskStateHistoryTests(unittest.TestCase):
    def test_explicit_ingress_task_id_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"EDGE_AGENT_STATE_DIR": temp}, clear=False):
                task_id = state.write_task_state(
                    role="claude", chat_id="chat-1", text="same request", status="started",
                    task_id="child-root-claude", root_task_id="root-1",
                )
                record = state.latest_task_state(chat_id="chat-1")
            self.assertEqual(task_id, "child-root-claude")
            self.assertEqual(record["task_id"], "child-root-claude")
            self.assertEqual(record["root_task_id"], "root-1")

    def test_history_has_ordered_timestamps_tails_and_redaction(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"EDGE_AGENT_STATE_DIR": temp}, clear=False):
                task_id = state.write_task_state(
                    role="codex",
                    chat_id="chat-1",
                    text="첫 부분 " + ("x" * 1100) + " 마지막 요청",
                    status="completed",
                    response_preview="앞부분",
                    response_tail="결과 마지막 token=do-not-store",
                )
                record = state.latest_task_state(role="codex", chat_id="chat-1")

            self.assertEqual(record["task_id"], task_id)
            self.assertGreater(record["sequence"], 0)
            self.assertTrue(record["updated_at"].endswith("+00:00"))
            self.assertTrue(record["request_tail"].endswith("마지막 요청"))
            self.assertNotIn("do-not-store", json.dumps(record, ensure_ascii=False))
            self.assertIn("[redacted]", record["response_tail"])
            self.assertEqual(len(Path(temp, "history.jsonl").read_text(encoding="utf-8").splitlines()), 1)

    def test_latest_uses_persisted_time_not_filename_or_mtime(self):
        with tempfile.TemporaryDirectory() as temp:
            history = Path(temp) / "history.jsonl"
            records = [
                {
                    "schema": "edge_agent.task_state_event.v2",
                    "task_id": "z-old",
                    "role": "codex",
                    "chat_id": "chat-1",
                    "status": "completed",
                    "updated_at": "2026-08-02T00:01:00+00:00",
                    "updated_epoch": 1785628860,
                    "sequence": 1,
                    "request_tail": "old",
                },
                {
                    "schema": "edge_agent.task_state_event.v2",
                    "task_id": "a-new",
                    "role": "codex",
                    "chat_id": "chat-1",
                    "status": "completed",
                    "updated_at": "2026-08-02T00:02:00+00:00",
                    "updated_epoch": 1785628920,
                    "sequence": 2,
                    "request_tail": "new",
                },
            ]
            history.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            with patch.dict(os.environ, {"EDGE_AGENT_STATE_DIR": temp}, clear=False):
                latest = state.latest_task_state(role="codex", chat_id="chat-1")

            self.assertEqual(latest["task_id"], "a-new")
            self.assertEqual(latest["selection"]["method"], "updated_at_then_sequence")
            self.assertEqual(latest["selection"]["candidate_count"], 2)

    def test_filters_prevent_cross_chat_latest_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"EDGE_AGENT_STATE_DIR": temp}, clear=False):
                state.write_task_state(role="codex", chat_id="chat-a", text="a", status="completed")
                state.write_task_state(role="codex", chat_id="chat-b", text="b", status="completed")
                latest = state.latest_task_state(role="codex", chat_id="chat-a")

            self.assertEqual(latest["chat_id"], "chat-a")
            self.assertEqual(latest["request_tail"], "a")

    def test_legacy_task_can_recover_exact_tail_from_telegram_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "telegram-delivery").mkdir()
            (root / "claude-task-1.json").write_text(json.dumps({
                "task_id": "claude-task-1",
                "role": "claude",
                "chat_id": "chat-1",
                "status": "completed",
                "updated_at": "2026-08-02T00:03:00+00:00",
                "delivery_id": "delivery-1",
                "response_preview": "앞부분만 보존됨",
            }), encoding="utf-8")
            (root / "telegram-delivery" / "delivery-1.json").write_text(json.dumps({
                "schema": "edge_agent.telegram_delivery.v1",
                "chunks": ["응답 앞부분", "정확한 마지막 부분 token=hidden"],
            }), encoding="utf-8")
            with patch.dict(os.environ, {"EDGE_AGENT_STATE_DIR": temp}, clear=False):
                latest = state.latest_task_state(role="claude", chat_id="chat-1")

            self.assertTrue(latest["response_tail"].endswith("[redacted]"))
            self.assertEqual(latest["response_tail_source"], "telegram_delivery")


if __name__ == "__main__":
    unittest.main()
