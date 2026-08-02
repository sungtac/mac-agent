import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import edge_agent_plan_gate as plan_gate  # noqa: E402
import edge_agent_state as task_state  # noqa: E402
from edge_agent_context_store import ContextStore  # noqa: E402
from edge_agent_secure_paths import SecurePathError  # noqa: E402
from edge_agent_session_lease import SessionLeaseManager  # noqa: E402
from edge_agent_parallel_locks import FileReservation  # noqa: E402
from edge_agent_telegram_delivery import DeliveryStoreError, create_delivery, load_delivery  # noqa: E402


class SecurePathIntegrationTests(unittest.TestCase):
    def test_managed_roots_reject_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside"
            outside.mkdir()
            link = root / "managed-link"
            link.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(SecurePathError):
                ContextStore(link).list_sessions()
            with self.assertRaises(SecurePathError):
                SessionLeaseManager(link)
            with self.assertRaises(SecurePathError):
                FileReservation(root / "repo", state_root=link)
            with patch.object(plan_gate, "PLAN_DIR", link):
                with self.assertRaises(SecurePathError):
                    plan_gate.save_pending(chat_id="1", task_id="t", request="r", plan="p", workspace="w")
            with patch.dict(os.environ, {"EDGE_AGENT_STATE_DIR": str(link)}, clear=False):
                with self.assertRaises(SecurePathError):
                    task_state.write_task_state(role="codex", chat_id="1", text="r", status="started")

    def test_delivery_does_not_follow_record_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "delivery"
            outside = Path(temp) / "outside.json"
            outside.write_text(json.dumps({"schema": "edge_agent.telegram_delivery.v1"}), encoding="utf-8")
            record = create_delivery(
                task_id="task-1", role="claude", chat_id="1", owner_user_id="1",
                source_message_id="10", session_id="", request_preview="r", chunks=["ok"], root=root,
            )
            target = root / f"{record['delivery_id']}.json"
            target.unlink()
            target.symlink_to(outside)
            with self.assertRaises(DeliveryStoreError):
                load_delivery(record["delivery_id"], root=root)


if __name__ == "__main__":
    unittest.main()
