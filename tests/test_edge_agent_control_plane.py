import os
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from edge_agent_control_plane import ControlPlaneError, ControlPlaneStore, verify_control_action  # noqa: E402


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = Path(self.temp.name) / "key"
        self.key.write_bytes(b"local-test-key-with-more-than-16-bytes")
        self.key.chmod(0o600)
        self.env = patch.dict(os.environ, {"EDGE_AGENT_MESSAGE_KEY_FILE": str(self.key)})
        self.env.start()
        self.store = ControlPlaneStore(Path(self.temp.name) / "control")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_approval_resolution_is_signed_and_durable(self):
        self.store.start_task("chat", "task-1", roles=("codex",))
        payload = self.store.request_approval("chat", "task-1", "approval-1")
        action = payload["events"][-1]["action"]
        self.assertTrue(verify_control_action(action))
        self.assertEqual(payload["status"], "waiting_approval")
        payload = self.store.resolve_approval("chat", "approval-1", approved=True)
        self.assertEqual(payload["tasks"]["task-1"]["status"], "running")

    def test_cancel_cascades_and_survives_new_store_instance(self):
        self.store.start_task("chat", "task-1")
        self.store.start_task("chat", "task-2")
        self.store.cancel_chat("chat", reason="사용자 취소")
        recovered = ControlPlaneStore(Path(self.temp.name) / "control").snapshot("chat")
        self.assertEqual(recovered["status"], "cancelled")
        self.assertTrue(all(task["status"] == "cancelled" for task in recovered["tasks"].values()))
        self.assertTrue(self.store.is_cancelled("chat", "task-1"))

    def test_cancelled_task_cannot_resume_without_approval(self):
        self.store.start_task("chat", "task-1")
        self.store.cancel_chat("chat", reason="stop")
        with self.assertRaises(ControlPlaneError):
            self.store.mark_task("chat", "task-1", "running")

    def test_recoverable_returns_waiting_control_state(self):
        self.store.start_task("chat", "task-1")
        self.store.request_approval("chat", "task-1", "approval-1")
        recovered = ControlPlaneStore(Path(self.temp.name) / "control").recoverable()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["status"], "waiting_approval")

    def test_new_task_reclaims_old_terminal_history_but_preserves_active_tasks(self):
        for index in range(8):
            task_id = f"old-{index}"
            self.store.start_task("chat", task_id)
            self.store.mark_task("chat", task_id, "completed", summary="done")

        self.store.start_task("chat", "new-task", roles=("codex",))
        payload = self.store.snapshot("chat")
        self.assertIn("new-task", payload["tasks"])
        self.assertEqual(payload["tasks"]["new-task"]["status"], "running")
        self.assertLessEqual(len(payload["tasks"]), 8)

    def test_task_cap_still_blocks_when_all_slots_are_active(self):
        for index in range(8):
            self.store.start_task("chat", f"active-{index}")

        with self.assertRaisesRegex(ControlPlaneError, "concurrency cap"):
            self.store.start_task("chat", "overflow")


if __name__ == "__main__":
    unittest.main()
