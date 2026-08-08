"""Explicit execution-to-automatic-merge pipeline.

Automatic merge is opt-in at construction time. Even when enabled, the
integrator still refuses dirty targets, undeclared changes, failed providers,
invalid diffs, and merge conflicts.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from edge_agent_parallel_executor import (
    ParallelExecutionResult,
    ParallelExecutor,
    ProviderOutcome,
)
from edge_agent_parallel_integrator import IntegrationResult, ParallelIntegrator
from edge_agent_parallel_worktree import ParallelTaskSpec, WorktreeManager


@dataclass(frozen=True)
class ParallelPipelineResult:
    execution: ParallelExecutionResult
    integration: IntegrationResult | None


class ParallelApprovalRequired(PermissionError):
    """Raised before provider execution when automatic merge lacks approval."""


class ParallelPipeline:
    """Run a provider and optionally integrate its passed result immediately."""

    def __init__(
        self,
        manager: WorktreeManager,
        *,
        parallel_enabled: bool = False,
        automatic_merge: bool = False,
        cleanup_after_merge: bool = True,
        approval_ref: str = "",
        approval_checker: Callable[[str, ParallelTaskSpec], bool] | None = None,
    ):
        self.executor = ParallelExecutor(manager, parallel_enabled=parallel_enabled)
        self.integrator = ParallelIntegrator(manager)
        self.automatic_merge = automatic_merge
        self.cleanup_after_merge = cleanup_after_merge
        self.approval_ref = approval_ref.strip()
        self.approval_checker = approval_checker

    def run(
        self,
        spec: ParallelTaskSpec,
        provider: Callable[[Path, ParallelTaskSpec], ProviderOutcome],
        *,
        event_writer: Callable[[dict], None] | None = None,
        require_diff: bool = True,
    ) -> ParallelPipelineResult:
        if self.automatic_merge:
            if not self.approval_ref or self.approval_checker is None:
                raise ParallelApprovalRequired(
                    "automatic merge requires an explicit approval reference and approval checker"
                )
            try:
                approved = bool(self.approval_checker(self.approval_ref, spec))
            except Exception as exc:
                raise ParallelApprovalRequired("approval verification failed closed") from exc
            if not approved:
                raise ParallelApprovalRequired("automatic merge approval is not granted")
        execution = self.executor.execute(
            spec,
            provider,
            event_writer=event_writer,
            require_diff=require_diff,
        )
        if execution.status != "succeeded":
            return ParallelPipelineResult(execution, None)
        integration = self.integrator.integrate(
            spec,
            execution,
            allow_merge=self.automatic_merge,
            cleanup_after_merge=self.cleanup_after_merge if self.automatic_merge else False,
        )
        return ParallelPipelineResult(execution, integration)
