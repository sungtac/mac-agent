import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_session_contract import (  # noqa: E402
    LogicalSession,
    Provider,
    SessionChannel,
    SessionStatus,
    load_logical_session,
    new_logical_session_id,
)


class LogicalSessionContractTests(unittest.TestCase):
    def make_session(self) -> LogicalSession:
        return LogicalSession(
            logical_session_id=new_logical_session_id(),
            task_id="task-20260731-session",
            channel=SessionChannel.TELEGRAM,
            provider=Provider.CLAUDE,
            workspace="/Users/edge_ai/mac-agent",
            worktree="/Users/edge_ai/.edge-agent-worktrees/task-20260731-session",
            owner="telegram",
            summary="작업 요약",
            decisions=["native provider 세션은 분리한다"],
            changed_files=["bin/example.py"],
            verification={"tier": "light", "passed": True},
        )

    def test_round_trip_preserves_provider_neutral_contract(self):
        session = self.make_session()
        session.bind_native_session("claude", "claude-native-1")
        payload = session.to_dict()
        restored = load_logical_session(payload)

        self.assertEqual(payload["schema"], "edge_agent.logical_session.v1")
        self.assertEqual(restored.logical_session_id, session.logical_session_id)
        self.assertEqual(restored.channel, SessionChannel.TELEGRAM)
        self.assertEqual(restored.native_sessions, {"claude": "claude-native-1"})
        self.assertEqual(restored.status, SessionStatus.CREATED)

    def test_provider_native_sessions_remain_separate(self):
        session = self.make_session()
        session.bind_native_session("claude", "claude-1")
        session.bind_native_session("codex", "codex-1")

        self.assertEqual(session.native_sessions, {"claude": "claude-1", "codex": "codex-1"})
        self.assertEqual(session.provider, Provider.CODEX)

    def test_invalid_schema_and_sensitive_handoff_are_rejected(self):
        with self.assertRaises(ValueError):
            load_logical_session({"schema": "wrong.v1"})

        with self.assertRaises(ValueError):
            LogicalSession(
                logical_session_id="sess-1",
                task_id="task-1",
                channel="terminal",
                summary="authorization: secret material must not be stored",
            )

    def test_summary_and_identifiers_are_bounded(self):
        with self.assertRaises(ValueError):
            LogicalSession(
                logical_session_id="bad id",
                task_id="task-1",
                channel="terminal",
            )

        with self.assertRaises(ValueError):
            LogicalSession(
                logical_session_id="sess-1",
                task_id="task-1",
                channel="terminal",
                summary="x" * 8001,
            )


if __name__ == "__main__":
    unittest.main()
