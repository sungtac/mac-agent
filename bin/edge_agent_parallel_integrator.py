"""Pipeline P4: single integration, rollback and conservative cleanup."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from edge_agent_parallel_locks import integration_lock
from edge_agent_parallel_worktree import ParallelTaskSpec, WorktreeManager
from edge_agent_parallel_executor import ParallelExecutionResult


@dataclass(frozen=True)
class IntegrationResult:
    status: str
    task_id: str
    commit: str = ""
    error_code: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "schema": "edge_agent_parallel_integration.v1",
            "status": self.status,
            "task_id": self.task_id,
            "commit": self.commit,
            "error_code": self.error_code,
            "error": self.error,
        }


def _git(cwd: Path, *args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


class ParallelIntegrator:
    def __init__(self, manager: WorktreeManager):
        self.manager = manager

    def _commit_worktree(self, spec: ParallelTaskSpec, result: ParallelExecutionResult) -> tuple[str, str]:
        worktree = Path(result.worktree_path)
        code, _, error = _git(worktree, "diff", "--check")
        if code != 0:
            return "", error[-1000:]
        code, _, error = _git(worktree, "add", "--", *spec.declared_files)
        if code != 0:
            return "", error[-1000:]
        code, _, error = _git(worktree, "diff", "--cached", "--quiet")
        if code == 0:
            return "", "worktree has no staged changes"
        code, _, error = _git(worktree, "commit", "-m", f"edge-agent: integrate {spec.task_id}")
        if code != 0:
            return "", error[-1000:]
        return _git(worktree, "rev-parse", "HEAD")[1], ""

    def integrate(
        self,
        spec: ParallelTaskSpec,
        result: ParallelExecutionResult,
        *,
        allow_merge: bool = False,
        cleanup_after_merge: bool = False,
    ) -> IntegrationResult:
        if result.status != "succeeded":
            return IntegrationResult("integration_blocked", spec.task_id, error_code="execution_not_passed", error="only a passed execution can be integrated")
        repo = Path(spec.repo_root)
        try:
            with integration_lock(repo, state_root=self.manager.state_root):
                if _git(repo, "status", "--porcelain")[1]:
                    return IntegrationResult("integration_blocked", spec.task_id, error_code="dirty_target", error="target checkout has uncommitted changes")
                current = _git(repo, "rev-parse", "HEAD")[1]
                code, _, error = _git(repo, "merge-base", "--is-ancestor", spec.base_commit, current)
                if code != 0:
                    return IntegrationResult("integration_blocked", spec.task_id, error_code="base_not_ancestor", error=error or "base commit is not an ancestor of target")
                existing_manifest = self.manager.read(spec.task_id)
                existing_commit = str(existing_manifest.result.get("commit", ""))
                if existing_commit and _git(Path(result.worktree_path), "status", "--porcelain")[1] == "":
                    commit, commit_error = existing_commit, ""
                else:
                    commit, commit_error = self._commit_worktree(spec, result)
                if not commit:
                    return IntegrationResult("integration_blocked", spec.task_id, error_code="worktree_commit_failed", error=commit_error)
                if not allow_merge:
                    self.manager.update(spec.task_id, state="merge_ready", result={"commit": commit})
                    return IntegrationResult("merge_ready", spec.task_id, commit=commit)
                code, _, error = _git(repo, "merge", "--no-ff", "--no-edit", commit)
                if code != 0:
                    # Never reset or auto-resolve a potentially conflicted target.
                    self.manager.update(spec.task_id, state="merge_ready", result={"commit": commit, "merge_error": error[-1000:]})
                    return IntegrationResult("merge_blocked", spec.task_id, commit=commit, error_code="merge_failed", error=error[-1000:])
                self.manager.update(spec.task_id, state="merged", result={"commit": commit})
                if cleanup_after_merge:
                    self.manager.remove_clean(spec.task_id)
                return IntegrationResult("merged", spec.task_id, commit=commit)
        except Exception as exc:
            return IntegrationResult("integration_blocked", spec.task_id, error_code="integration_exception", error=str(exc)[-2000:])

    def rollback_unmerged(self, task_id: str) -> IntegrationResult:
        manifest = self.manager.read(task_id)
        worktree = Path(manifest.worktree_path)
        if worktree.exists() and _git(worktree, "status", "--porcelain")[1]:
            self.manager.update(task_id, state="cancelled", result={"cleanup": "preserved_dirty_worktree"})
            return IntegrationResult("rollback_preserved", task_id, error_code="dirty_worktree", error="dirty worktree preserved for manual review")
        self.manager.update(task_id, state="cancelled", result={"cleanup": "removed_clean_worktree"})
        self.manager.remove_clean(task_id)
        return IntegrationResult("rolled_back", task_id)
