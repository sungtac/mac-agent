"""Pipeline P3: provider execution and evidence/result handoff.

The executor is provider-neutral. Production callers must explicitly enable the
parallel mode and supply a provider callback; the default remains disabled.
"""
from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from edge_agent_parallel_locks import FileReservation, task_lock
from edge_agent_parallel_worktree import ParallelTaskSpec, WorktreeManager, WorktreeManifest


RESERVATION_HEARTBEAT_SECONDS = max(
    1.0,
    float(os.environ.get("EDGE_AGENT_RESERVATION_HEARTBEAT_SECONDS", "300")),
)


class ParallelExecutionDisabled(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderOutcome:
    ok: bool
    output: str = ""
    exit_code: int = 0
    error: str = ""
    verification: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ParallelExecutionResult:
    schema: str
    task_id: str
    status: str
    worktree_path: str
    base_commit: str
    changed_files: tuple[str, ...] = ()
    unexpected_files: tuple[str, ...] = ()
    exit_code: int = 0
    output: str = ""
    error_code: str = ""
    error: str = ""
    verification: dict = field(default_factory=dict)
    event_idempotency_key: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _git(cwd: Path, *args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode, (result.stdout or ""), (result.stderr or "")


def _changed_files(worktree: Path) -> tuple[str, ...]:
    _, tracked, _ = _git(worktree, "diff", "--name-only")
    _, staged, _ = _git(worktree, "diff", "--cached", "--name-only")
    _, untracked, _ = _git(worktree, "ls-files", "--others", "--exclude-standard")
    values = []
    for output in (tracked, staged, untracked):
        values.extend(line.strip().replace("\\", "/") for line in output.splitlines() if line.strip())
    return tuple(dict.fromkeys(values))


def _unexpected(changed: tuple[str, ...], declared: tuple[str, ...]) -> tuple[str, ...]:
    def overlaps(path: str, declared_path: str) -> bool:
        return path == declared_path or path.startswith(f"{declared_path}/") or declared_path.startswith(f"{path}/")
    return tuple(path for path in changed if not any(overlaps(path, target) for target in declared))


def _heartbeat_loop(reservation: FileReservation, task_id: str, stop: threading.Event) -> None:
    while not stop.wait(RESERVATION_HEARTBEAT_SECONDS):
        try:
            reservation.heartbeat(task_id)
        except (OSError, ValueError):
            # The provider result remains authoritative; audit will report a
            # stale reservation if the registry cannot be refreshed.
            continue


class ParallelExecutor:
    def __init__(self, manager: WorktreeManager, *, parallel_enabled: bool = False):
        self.manager = manager
        self.parallel_enabled = parallel_enabled

    def execute(
        self,
        spec: ParallelTaskSpec,
        provider: Callable[[Path, ParallelTaskSpec], ProviderOutcome],
        *,
        event_writer: Callable[[dict], None] | None = None,
        require_diff: bool = True,
    ) -> ParallelExecutionResult:
        if not self.parallel_enabled:
            raise ParallelExecutionDisabled("parallel provider execution is disabled by default")
        manifest: WorktreeManifest = self.manager.read(spec.task_id)
        worktree = Path(manifest.worktree_path)
        reservation = FileReservation(spec.repo_root, state_root=self.manager.state_root)
        reservation.reserve(
            task_id=spec.task_id,
            files=spec.declared_files,
            dependency_keys=spec.dependency_keys,
            owner=spec.owner,
        )
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(reservation, spec.task_id, heartbeat_stop),
            name=f"edge-agent-reservation-heartbeat-{spec.task_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        event_key = f"{spec.task_id}::execution"
        try:
            with task_lock(spec.task_id, state_root=self.manager.state_root):
                self.manager.update(spec.task_id, state="running")
                try:
                    outcome = provider(worktree, spec)
                    changed = _changed_files(worktree)
                    unexpected = _unexpected(changed, spec.declared_files)
                    _, _, diff_error = _git(worktree, "diff", "--check")
                    error_code = ""
                    error = outcome.error
                    status = "succeeded"
                    if not outcome.ok:
                        status, error_code, error = "failed", "provider_failed", outcome.error or "provider returned failure"
                    elif unexpected:
                        status, error_code, error = "failed", "unexpected_files", f"provider changed undeclared files: {', '.join(unexpected)}"
                    elif diff_error.strip():
                        status, error_code, error = "failed", "diff_check_failed", diff_error.strip()[-1000:]
                    elif require_diff and not changed:
                        status, error_code, error = "failed", "no_changes", "provider completed without a real diff"
                    result = ParallelExecutionResult(
                        schema="edge_agent_parallel_execution.v1",
                        task_id=spec.task_id,
                        status=status,
                        worktree_path=str(worktree),
                        base_commit=manifest.base_commit,
                        changed_files=changed,
                        unexpected_files=unexpected,
                        exit_code=outcome.exit_code,
                        output=outcome.output[-4000:],
                        error_code=error_code,
                        error=error[-2000:] if error else "",
                        verification=dict(outcome.verification),
                        event_idempotency_key=event_key,
                    )
                    if event_writer is not None:
                        event_writer(result.to_dict())
                    self.manager.update(spec.task_id, state=status, result=result.to_dict())
                    return result
                except Exception as exc:
                    result = ParallelExecutionResult(
                        schema="edge_agent_parallel_execution.v1",
                        task_id=spec.task_id,
                        status="failed",
                        worktree_path=str(worktree),
                        base_commit=manifest.base_commit,
                        error_code="executor_exception",
                        error=str(exc)[-2000:],
                        event_idempotency_key=event_key,
                    )
                    self.manager.update(spec.task_id, state="failed", result=result.to_dict())
                    return result
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=min(RESERVATION_HEARTBEAT_SECONDS, 1.0))
            reservation.release(spec.task_id)
