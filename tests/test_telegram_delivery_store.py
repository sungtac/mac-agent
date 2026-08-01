#!/usr/bin/env python3
"""Tests for the Telegram delivery outbox."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_telegram_delivery import (  # noqa: E402
    DeliveryStoreError,
    create_delivery,
    list_pending_deliveries,
    load_delivery,
    mark_chunk_sent,
    mark_delivery_succeeded,
    pending_indexes,
)


class TelegramDeliveryStoreTests(unittest.TestCase):
    def test_partial_delivery_is_resumable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            record = create_delivery(
                task_id="task-1", role="claude", chat_id="-1", owner_user_id="7",
                source_message_id="10", session_id="sess-task-1", request_preview="요청",
                chunks=["a", "b"], workspace="/tmp/task-1", root=directory,
            )
            self.assertEqual(record["task_id"], "task-1")
            self.assertEqual(record["workspace"], "/tmp/task-1")
            self.assertEqual(pending_indexes(record), [0, 1])
            delivery_id = record["delivery_id"]
            mark_chunk_sent(delivery_id, 0, 101, root=directory)
            mark_chunk_sent(delivery_id, 0, 999, root=directory)
            current = load_delivery(delivery_id, root=directory)
            self.assertEqual(current["sent_message_ids"], {"0": "101"})
            self.assertEqual(pending_indexes(current), [1])
            mark_chunk_sent(delivery_id, 1, 102, root=directory)
            mark_delivery_succeeded(delivery_id, root=directory)
            self.assertEqual(load_delivery(delivery_id, root=directory)["status"], "succeeded")

    def test_retry_is_scoped_to_chat_role_and_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            create_delivery(
                task_id="task-1", role="claude", chat_id="-1", owner_user_id="7",
                source_message_id="10", session_id="", request_preview="요청",
                chunks=["a"], root=directory,
            )
            self.assertEqual(
                len(list_pending_deliveries(chat_id="-1", role="claude", owner_user_id="7", root=directory)),
                1,
            )
            self.assertEqual(
                list_pending_deliveries(chat_id="-1", role="claude", owner_user_id="8", root=directory),
                [],
            )
            self.assertEqual(
                list_pending_deliveries(chat_id="-2", role="claude", owner_user_id="7", root=directory),
                [],
            )

    def test_expired_delivery_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            created = datetime.now(timezone.utc) - timedelta(hours=2)
            create_delivery(
                task_id="task-1", role="claude", chat_id="-1", owner_user_id="7",
                source_message_id="10", session_id="", request_preview="요청",
                chunks=["a"], root=directory, now=created, ttl_seconds=300,
            )
            delivery_id = f"task-1-10"
            self.assertEqual(load_delivery(delivery_id, root=directory)["status"], "expired")
            with self.assertRaises(DeliveryStoreError):
                mark_delivery_succeeded(delivery_id, root=directory)
            self.assertEqual(
                list_pending_deliveries(chat_id="-1", role="claude", owner_user_id="7", root=directory),
                [],
            )

    def test_files_are_private_and_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            create_delivery(
                task_id="task-1", role="claude", chat_id="-1", owner_user_id="7",
                source_message_id="10", session_id="", request_preview="요청",
                chunks=["a"], root=directory,
            )
            path = Path(directory) / "task-1-10.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text("{not-json}\n", encoding="utf-8")
            with self.assertRaises(DeliveryStoreError):
                load_delivery("task-1", root=directory)

    def test_delivery_id_collision_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            create_delivery(
                task_id="task-1", role="claude", chat_id="-1", owner_user_id="7",
                source_message_id="10", session_id="", request_preview="요청",
                chunks=["a"], delivery_id="shared-id", root=directory,
            )
            with self.assertRaises(DeliveryStoreError):
                create_delivery(
                    task_id="task-2", role="claude", chat_id="-1", owner_user_id="7",
                    source_message_id="11", session_id="", request_preview="다른 요청",
                    chunks=["b"], delivery_id="shared-id", root=directory,
                )

    def test_invalid_expiry_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            record = create_delivery(
                task_id="task-1", role="claude", chat_id="-1", owner_user_id="7",
                source_message_id="10", session_id="", request_preview="요청",
                chunks=["a"], root=directory,
            )
            path = Path(directory) / f"{record['delivery_id']}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["expires_at"] = "not-a-date"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(DeliveryStoreError):
                load_delivery(record["delivery_id"], root=directory)


if __name__ == "__main__":
    unittest.main()
