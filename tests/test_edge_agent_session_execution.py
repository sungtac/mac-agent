import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_context_store import ContextStore  # noqa: E402
from edge_agent_parallel_executor import ProviderOutcome  # noqa: E402
from edge_agent_parallel_worktree import ParallelTaskSpec, WorktreeManager  # noqa: E402
from edge_agent_session_contract import LogicalSession, SessionStatus, new_logical_session_id  # noqa: E402
from edge_agent_session_execution import SessionParallelRunner, SessionTaskMismatch  # noqa: E402
from edge_agent_session_lease import SessionLeaseManager  # noqa: E402


class SessionParallelRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="edge-agent-session-exec-")
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "edge-agent-test@example.invalid")
        self.git("config", "user.name", "Edge Agent Test")
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")
        self.git("add", "a.txt")
        self.git("commit", "-qm", "baseline")
        self.base = self.git("rev-parse", "HEAD")
        self.state = Path(self.temp.name) / "parallel-state"
        self.worktrees = Path(self.temp.name) / "worktrees"
        self.sessions = Path(self.temp.name) / "sessions"
        self.manager = WorktreeManager(state_root=self.state, worktree_root=self.worktrees)
        self.store = ContextStore(self.sessions)
        self.leases = SessionLeaseManager(Path(self.temp.name) / "leases")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["/usr/bin/git", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout.strip()

    def make_session(self, task_id="task-1"):
        session = LogicalSession(
            logical_session_id=new_logical_session_id(),
            task_id=task_id,
            channel="terminal",
            owner="terminal",
            base_commit=self.base,
        )
        self.store.create(session)
        return session

    def spec(self, task_id="task-1"):
        return ParallelTaskSpec(
            repo_root=str(self.root),
            base_commit=self.base,
            declared_files=("a.txt",),
            task_id=task_id,
        )

    def test_runner_binds_session_lease_worktree_and_result(self):
        session = self.make_session()

        def provider(worktree, _spec):
            (worktree / "a.txt").write_text("changed\n", encoding="utf-8")
            return ProviderOutcome(ok=True, verification={"tests": "pass"})

        result = SessionParallelRunner(self.manager, self.store, self.leases).run(
            session,
            self.spec(),
            provider,
            owner="terminal",
            parallel_enabled=True,
        )
        self.assertEqual(result.pipeline.execution.status, "succeeded")
        self.assertEqual(result.session.status, SessionStatus.HANDOFF_READY)
        self.assertEqual(result.session.changed_files, ["a.txt"])
        self.assertEqual(result.adapter_result.status, "blocked")
        self.assertEqual(result.adapter_result.integration_status, "merge_ready")
        self.assertEqual([item["event_type"] for item in self.store.events(session.logical_session_id)], [
            "session_created", "execution_started", "worktree_created", "execution_completed"
        ])
        self.assertEqual(self.leases.current_metadata(session.logical_session_id)["state"], "released")

    def test_runner_rejects_session_task_mismatch_before_worktree_creation(self):
        session = self.make_session("task-one")
        with self.assertRaises(SessionTaskMismatch):
            SessionParallelRunner(self.manager, self.store, self.leases).run(
                session,
                self.spec("task-two"),
                lambda *_: ProviderOutcome(ok=True),
                owner="terminal",
                parallel_enabled=True,
            )
        self.assertEqual(list(self.worktrees.rglob("*")), [])

    def test_automatic_merge_is_the_only_path_that_returns_passed(self):
        session = self.make_session("task-merge")

        def provider(worktree, _spec):
            (worktree / "a.txt").write_text("merged\n", encoding="utf-8")
            return ProviderOutcome(ok=True, verification={"tier": "light", "tests": "pass"})

        result = SessionParallelRunner(self.manager, self.store, self.leases).run(
            session,
            self.spec("task-merge"),
            provider,
            owner="terminal",
            parallel_enabled=True,
            automatic_merge=True,
            approval_ref="approval-task-merge",
            approval_checker=lambda ref, _spec: ref == "approval-task-merge",
        )
        self.assertEqual(result.pipeline.integration.status, "merged")
        self.assertEqual(result.session.status, SessionStatus.SUCCEEDED)
        self.assertEqual(result.adapter_result.status, "passed")
        self.assertEqual(result.adapter_result.commit != "", True)


if __name__ == "__main__":
    unittest.main()
