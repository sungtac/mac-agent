import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_context_store import ContextStore  # noqa: E402
from edge_agent_session_contract import LogicalSession, SessionStatus, new_logical_session_id  # noqa: E402


class ContextStoreTests(unittest.TestCase):
    def make_session(self) -> LogicalSession:
        return LogicalSession(
            logical_session_id=new_logical_session_id(),
            task_id="task-context-1",
            channel="terminal",
            owner="terminal",
            summary="초기 조사 결과",
            decisions=["native 세션은 provider별로 유지"],
            risk_notes=["동시 인계 시 lease 확인 필요"],
            changed_files=["bin/example.py"],
            verification={"passed": False},
        )

    def test_create_load_and_append_event(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ContextStore(temp)
            session = self.make_session()
            store.create(session)
            event = store.append_event(session.logical_session_id, "checkpoint", {"step": "context"})

            loaded = store.load(session.logical_session_id)
            self.assertEqual(loaded.task_id, session.task_id)
            self.assertEqual([item["event_type"] for item in store.events(session.logical_session_id)], ["session_created", "checkpoint"])
            self.assertTrue(event["event_id"].startswith("evt-"))

    def test_handoff_atomically_updates_snapshot_and_context(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ContextStore(temp, max_context_chars=1200)
            session = self.make_session()
            store.create(session)
            updated = store.record_handoff(
                session.logical_session_id,
                target_channel="telegram",
                summary="터미널에서 조사 완료",
                next_action="Telegram에서 검토 후 승인",
                reason="사용자 채널 전환",
            )

            self.assertEqual(updated.status, SessionStatus.HANDOFF_READY)
            self.assertEqual(store.load(session.logical_session_id).channel.value, "telegram")
            context = store.bounded_context(session.logical_session_id)
            self.assertLessEqual(len(context), 1200)
            self.assertIn("터미널에서 조사 완료", context)
            self.assertIn("provider native 세션을 병합하지 않는다", context)
            self.assertEqual(store.events(session.logical_session_id)[-1]["event_type"], "session_handoff")

    def test_sensitive_event_and_duplicate_session_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ContextStore(temp)
            session = self.make_session()
            store.create(session)
            with self.assertRaises(ValueError):
                store.append_event(session.logical_session_id, "bad", {"note": "token=do-not-store"})
            with self.assertRaises(FileExistsError):
                store.create(session)
            self.assertEqual(len(store.events(session.logical_session_id)), 1)

    def test_corrupt_snapshot_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ContextStore(temp)
            session = self.make_session()
            store.create(session)
            snapshot = Path(temp) / "snapshots" / f"{session.logical_session_id}.json"
            snapshot.write_text("not-json\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                store.load(session.logical_session_id)


if __name__ == "__main__":
    unittest.main()
