"""Pipeline P5: token-free contract tests for the parallel worktree plan."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from edge_agent_parallel_executor import (  # noqa: E402
    ParallelExecutionDisabled,
    ParallelExecutor,
    ProviderOutcome,
)
import edge_agent_parallel_executor as executor_module  # noqa: E402
from edge_agent_parallel_integrator import ParallelIntegrator  # noqa: E402
from edge_agent_parallel_audit import audit  # noqa: E402
from edge_agent_parallel_pipeline import ParallelPipeline  # noqa: E402
from edge_agent_parallel_locks import FileReservation, ReservationConflict, reservation_is_stale  # noqa: E402
from edge_agent_parallel_worktree import (  # noqa: E402
    ParallelTaskSpec,
    SourceDirtyError,
    WorktreeManager,
)


class ParallelPipelineContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="edge-agent-parallel-contract-")
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "edge-agent-test@example.invalid")
        self._git("config", "user.name", "Edge Agent Test")
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")
        (self.root / "b.txt").write_text("b\n", encoding="utf-8")
        self._git("add", "a.txt", "b.txt")
        self._git("commit", "-qm", "baseline")
        self.base = self._git("rev-parse", "HEAD")
        self.state = Path(self.temp.name) / "state"
        self.worktrees = Path(self.temp.name) / "worktrees"
        self.manager = WorktreeManager(state_root=self.state, worktree_root=self.worktrees)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _git(self, *args: str) -> str:
        import subprocess

        result = subprocess.run(["/usr/bin/git", "-C", str(self.root), *args], check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def _spec(self, task_id: str, filename: str) -> ParallelTaskSpec:
        return ParallelTaskSpec(
            repo_root=str(self.root),
            base_commit=self.base,
            declared_files=(filename,),
            task_id=task_id,
        )

    def test_worktree_is_clean_and_duplicate_task_is_rejected(self):
        spec = self._spec("task-a", "a.txt")
        manifest = self.manager.create(spec)
        self.assertEqual(manifest.state, "created")
        self.assertEqual((Path(manifest.worktree_path) / "a.txt").read_text(), "a\n")
        with self.assertRaises(FileExistsError):
            self.manager.create(spec)

    def test_dirty_source_is_refused(self):
        (self.root / "a.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(SourceDirtyError):
            self.manager.create(self._spec("dirty", "a.txt"))

    def test_non_overlapping_reservations_can_coexist(self):
        first = FileReservation(self.root, state_root=self.state)
        second = FileReservation(self.root, state_root=self.state)
        first.reserve(task_id="one", files=("a.txt",))
        second.reserve(task_id="two", files=("b.txt",))
        self.assertEqual({item["task_id"] for item in first.active()}, {"one", "two"})
        first.release("one")
        second.release("two")

    def test_overlapping_reservation_is_rejected(self):
        reservation = FileReservation(self.root, state_root=self.state)
        reservation.reserve(task_id="one", files=("src",))
        with self.assertRaises(ReservationConflict):
            reservation.reserve(task_id="two", files=("src/module.py",))

    def test_reservation_heartbeat_refreshes_active_record(self):
        reservation = FileReservation(self.root, state_root=self.state)
        record = reservation.reserve(task_id="heartbeat", files=("a.txt",))
        self.assertIn("heartbeat_at", record)
        self.assertFalse(reservation_is_stale(record, ttl_seconds=3600))
        self.assertTrue(reservation.heartbeat("heartbeat"))
        self.assertFalse(reservation.heartbeat("missing"))

    def test_stale_active_reservation_is_quarantined_before_conflict_check(self):
        reservation = FileReservation(self.root, state_root=self.state, ttl_seconds=60)
        reservation.registry.parent.mkdir(parents=True, exist_ok=True)
        reservation.registry.write_text(json.dumps([{
            "schema": "edge_agent_parallel_reservation.v1",
            "task_id": "stale-task",
            "repo_root": str(self.root),
            "owner": "test",
            "files": ["a.txt"],
            "dependency_keys": [],
            "state": "active",
            "created_at": "2000-01-01T00:00:00+00:00",
            "heartbeat_at": "2000-01-01T00:00:00+00:00",
        }]), encoding="utf-8")
        created = reservation.reserve(task_id="new-task", files=("a.txt",))
        self.assertEqual(created["task_id"], "new-task")
        records = json.loads(reservation.registry.read_text(encoding="utf-8"))
        self.assertEqual({record["state"] for record in records}, {"stale", "active"})

    def test_executor_starts_and_stops_reservation_heartbeat(self):
        spec = self._spec("heartbeat-executor", "a.txt")
        self.manager.create(spec)
        heartbeat_calls = []
        original_heartbeat = FileReservation.heartbeat
        original_interval = executor_module.RESERVATION_HEARTBEAT_SECONDS

        def recording_heartbeat(instance, task_id):
            heartbeat_calls.append(task_id)
            return original_heartbeat(instance, task_id)

        FileReservation.heartbeat = recording_heartbeat
        executor_module.RESERVATION_HEARTBEAT_SECONDS = 0.01
        try:
            result = ParallelExecutor(self.manager, parallel_enabled=True).execute(
                spec,
                lambda worktree, _spec: (
                    (worktree / "a.txt").write_text("changed\n", encoding="utf-8"),
                    time.sleep(0.03),
                    ProviderOutcome(ok=True, output="done"),
                )[-1],
            )
        finally:
            FileReservation.heartbeat = original_heartbeat
            executor_module.RESERVATION_HEARTBEAT_SECONDS = original_interval
        self.assertEqual(result.status, "succeeded")
        self.assertIn("heartbeat-executor", heartbeat_calls)

    def test_read_only_audit_reports_stale_reservation(self):
        reservation = FileReservation(self.root, state_root=self.state)
        reservation.reserve(task_id="stale-task", files=("a.txt",))
        payload = json.loads(reservation.registry.read_text(encoding="utf-8"))
        payload[0]["heartbeat_at"] = "2000-01-01T00:00:00+00:00"
        reservation.registry.write_text(json.dumps(payload), encoding="utf-8")
        findings = audit(self.manager, self.root, reservation_ttl_seconds=1)
        stale = [finding for finding in findings if finding.code == "stale_reservation"]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].task_id, "stale-task")
        self.assertIn("human review required", stale[0].detail)

    def test_executor_is_disabled_by_default(self):
        spec = self._spec("disabled", "a.txt")
        self.manager.create(spec)
        with self.assertRaises(ParallelExecutionDisabled):
            ParallelExecutor(self.manager).execute(spec, lambda *_: ProviderOutcome(ok=True))

    def test_non_overlapping_tasks_execute_concurrently_when_explicitly_enabled(self):
        specs = (self._spec("parallel-a", "a.txt"), self._spec("parallel-b", "b.txt"))
        for spec in specs:
            self.manager.create(spec)
        rendezvous = Barrier(2)
        events = []

        def provider(worktree: Path, spec: ParallelTaskSpec) -> ProviderOutcome:
            rendezvous.wait(timeout=5)
            target = worktree / spec.declared_files[0]
            target.write_text(f"{spec.task_id}\n", encoding="utf-8")
            return ProviderOutcome(ok=True, verification={"provider": "fake-concurrent"})

        executor = ParallelExecutor(self.manager, parallel_enabled=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(executor.execute, spec, provider, event_writer=events.append) for spec in specs]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual([result.status for result in results], ["succeeded", "succeeded"])
        self.assertEqual({result.changed_files for result in results}, {("a.txt",), ("b.txt",)})
        self.assertEqual({event["task_id"] for event in events}, {"parallel-a", "parallel-b"})

    def test_read_only_audit_accepts_registered_worktree(self):
        spec = self._spec("audited", "a.txt")
        manifest = self.manager.create(spec)
        findings = audit(self.manager, self.root)
        self.assertEqual(findings, [])
        self.manager.update(manifest.task_id, state="cancelled")
        self.manager.remove_clean(manifest.task_id)

    def test_fake_provider_result_requires_real_diff_and_records_event(self):
        spec = self._spec("execute", "a.txt")
        self.manager.create(spec)
        events = []

        def provider(worktree: Path, _spec: ParallelTaskSpec) -> ProviderOutcome:
            (worktree / "a.txt").write_text("changed\n", encoding="utf-8")
            return ProviderOutcome(ok=True, output="fake provider", verification={"tests": "pass"})

        result = ParallelExecutor(self.manager, parallel_enabled=True).execute(spec, provider, event_writer=events.append)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.changed_files, ("a.txt",))
        self.assertEqual(events[0]["event_idempotency_key"], "execute::execution")

    def test_no_change_is_not_success(self):
        spec = self._spec("noop", "a.txt")
        self.manager.create(spec)
        result = ParallelExecutor(self.manager, parallel_enabled=True).execute(
            spec,
            lambda *_: ProviderOutcome(ok=True, output="done"),
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "no_changes")

    def test_merge_is_opt_in_and_cleanup_is_explicit(self):
        spec = self._spec("merge", "a.txt")
        self.manager.create(spec)

        def provider(worktree: Path, _spec: ParallelTaskSpec) -> ProviderOutcome:
            (worktree / "a.txt").write_text("merged\n", encoding="utf-8")
            return ProviderOutcome(ok=True)

        result = ParallelExecutor(self.manager, parallel_enabled=True).execute(spec, provider)
        ready = ParallelIntegrator(self.manager).integrate(spec, result)
        self.assertEqual(ready.status, "merge_ready")
        self.assertEqual((self.root / "a.txt").read_text(), "a\n")
        merged = ParallelIntegrator(self.manager).integrate(spec, result, allow_merge=True, cleanup_after_merge=True)
        self.assertEqual(merged.status, "merged")
        self.assertEqual((self.root / "a.txt").read_text(), "merged\n")
        self.assertFalse(Path(result.worktree_path).exists())

    def test_automatic_pipeline_merges_only_after_passed_execution(self):
        spec = self._spec("automatic", "a.txt")
        self.manager.create(spec)

        def provider(worktree: Path, _spec: ParallelTaskSpec) -> ProviderOutcome:
            (worktree / "a.txt").write_text("automatic\n", encoding="utf-8")
            return ProviderOutcome(ok=True, verification={"tests": "pass"})

        outcome = ParallelPipeline(
            self.manager,
            parallel_enabled=True,
            automatic_merge=True,
        ).run(spec, provider)
        self.assertEqual(outcome.execution.status, "succeeded")
        self.assertIsNotNone(outcome.integration)
        self.assertEqual(outcome.integration.status, "merged")
        self.assertEqual((self.root / "a.txt").read_text(), "automatic\n")
        self.assertFalse(Path(outcome.execution.worktree_path).exists())

    def test_automatic_pipeline_blocks_on_dirty_target_without_overwriting_it(self):
        spec = self._spec("dirty-target", "a.txt")
        self.manager.create(spec)

        def provider(worktree: Path, _spec: ParallelTaskSpec) -> ProviderOutcome:
            (worktree / "a.txt").write_text("provider\n", encoding="utf-8")
            (self.root / "b.txt").write_text("user-change\n", encoding="utf-8")
            return ProviderOutcome(ok=True)

        outcome = ParallelPipeline(
            self.manager,
            parallel_enabled=True,
            automatic_merge=True,
        ).run(spec, provider)
        self.assertEqual(outcome.execution.status, "succeeded")
        self.assertEqual(outcome.integration.status, "integration_blocked")
        self.assertEqual(outcome.integration.error_code, "dirty_target")
        self.assertEqual((self.root / "b.txt").read_text(), "user-change\n")
        self.assertEqual((self.root / "a.txt").read_text(), "a\n")
        self.assertTrue(Path(outcome.execution.worktree_path).exists())


if __name__ == "__main__":
    unittest.main()
