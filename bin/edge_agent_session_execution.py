#!/usr/bin/env python3
"""Provider-neutral bridge between logical sessions and parallel worktrees.

This is an opt-in harness layer.  It requires an existing ContextStore
snapshot, a declared task scope, and an explicit ``parallel_enabled=True``.
It does not attach itself to Telegram, Discord, launchd, or Team OS.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from edge_agent_adapter_contract import EdgeAgentResult
from edge_agent_context_store import ContextStore
from edge_agent_parallel_executor import ProviderOutcome
from edge_agent_parallel_pipeline import ParallelPipeline, ParallelPipelineResult
from edge_agent_parallel_worktree import ParallelTaskSpec, WorktreeManager
from edge_agent_session_contract import LogicalSession, SessionStatus
from edge_agent_session_lease import SessionLeaseManager


class SessionTaskMismatch(ValueError):
    pass


@dataclass(frozen=True)
class SessionExecutionResult:
    session: LogicalSession
    pipeline: ParallelPipelineResult
    adapter_result: EdgeAgentResult


class SessionParallelRunner:
    """Run one declared worktree task under one logical-session lease."""

    def __init__(self, manager: WorktreeManager, store: ContextStore, leases: SessionLeaseManager):
        self.manager = manager
        self.store = store
        self.leases = leases

    @staticmethod
    def _validate_binding(session: LogicalSession, spec: ParallelTaskSpec) -> None:
        if session.task_id != spec.task_id:
            raise SessionTaskMismatch("session task_id and parallel task_id differ")
        if session.base_commit and session.base_commit != spec.base_commit:
            raise SessionTaskMismatch("session base_commit and parallel task base_commit differ")
        if session.status in {SessionStatus.SUCCEEDED, SessionStatus.CANCELLED}:
            raise SessionTaskMismatch(f"terminal session cannot execute: {session.status.value}")

    def run(
        self,
        session: LogicalSession,
        spec: ParallelTaskSpec,
        provider: Callable[[Path, ParallelTaskSpec], ProviderOutcome],
        *,
        owner: str,
        parallel_enabled: bool = False,
        automatic_merge: bool = False,
        approval_ref: str = "",
        approval_checker: Callable[[str, ParallelTaskSpec], bool] | None = None,
        require_diff: bool = True,
    ) -> SessionExecutionResult:
        if not parallel_enabled:
            raise ValueError("session parallel execution is disabled unless explicitly enabled")
        self._validate_binding(session, spec)
        persisted = self.store.load(session.logical_session_id)
        self._validate_binding(persisted, spec)

        with self.leases.acquire(session.logical_session_id, owner):
            persisted.status = SessionStatus.RUNNING
            persisted.owner = owner
            self.store.save(persisted, event_type="execution_started", payload={"task_id": spec.task_id})
            try:
                manifest = self.manager.create(spec)
                persisted.worktree = manifest.worktree_path
                persisted.base_commit = manifest.base_commit
                self.store.save(
                    persisted,
                    event_type="worktree_created",
                    payload={"worktree": manifest.worktree_path, "base_commit": manifest.base_commit},
                )
                pipeline = ParallelPipeline(
                    self.manager,
                    parallel_enabled=True,
                    automatic_merge=automatic_merge,
                    cleanup_after_merge=automatic_merge,
                    approval_ref=approval_ref,
                    approval_checker=approval_checker,
                ).run(spec, provider, require_diff=require_diff)
            except Exception as exc:
                persisted.status = SessionStatus.FAILED
                persisted.next_action = "worktree/provider 오류 원인 확인"
                self.store.save(persisted, event_type="execution_failed", payload={"error_code": type(exc).__name__})
                raise

            execution = pipeline.execution
            integration = pipeline.integration
            integration_status = integration.status if integration is not None else ""
            if execution.status != "succeeded":
                persisted.status = SessionStatus.FAILED
            elif integration_status == "merged":
                persisted.status = SessionStatus.SUCCEEDED
            else:
                persisted.status = SessionStatus.HANDOFF_READY
            persisted.changed_files = list(execution.changed_files)
            persisted.verification = dict(execution.verification)
            persisted.next_action = "병합 검토" if integration_status in {"merge_ready", "integration_blocked", "merge_blocked"} else "실패 원인 확인"
            if integration_status == "merged":
                persisted.next_action = "사용자에게 결과 전달"
            adapter_status = "failed"
            if execution.status == "succeeded":
                adapter_status = "passed" if integration_status == "merged" else "blocked"
            adapter_result = EdgeAgentResult(
                request_id=persisted.request_id or persisted.task_id,
                task_id=persisted.task_id,
                logical_session_id=persisted.logical_session_id,
                status=adapter_status,
                execution_status=execution.status,
                integration_status=integration_status,
                commit=integration.commit if integration else "",
                changed_files=tuple(execution.changed_files),
                verification_tier=str(execution.verification.get("tier", "unclassified")),
                event_idempotency_key=execution.event_idempotency_key,
                provider=persisted.provider.value if persisted.provider else "",
                evidence_refs=(f"event://{execution.event_idempotency_key}",) if execution.event_idempotency_key else (),
                error_code=execution.error_code or (integration.error_code if integration else ""),
                next_action=persisted.next_action,
            )
            self.store.save(
                persisted,
                event_type="execution_completed",
                payload={
                    "status": execution.status,
                    "integration_status": integration_status,
                    "changed_files": list(execution.changed_files),
                    "event_idempotency_key": execution.event_idempotency_key,
                },
            )
            return SessionExecutionResult(persisted, pipeline, adapter_result)
