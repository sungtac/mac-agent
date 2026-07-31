"""Pipeline P1: isolated worktree lifecycle and task manifest.

This module is deliberately opt-in. It creates clean detached worktrees from an
explicit base commit, never copies a dirty checkout, and never merges or deletes
an active task implicitly.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SCHEMA = "edge_agent_parallel_worktree.v1"
# `succeeded` and `merge_ready` still own a live worktree and therefore remain
# recoverable states until integration or explicit cancellation finishes.
TERMINAL_STATES = {"failed", "cancelled", "merged"}
ACTIVE_STATES = {"created", "running", "succeeded", "merge_ready"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail[-500:]}")
    return (result.stdout or "").strip()


def _canonical_repo(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    top = _git(resolved, "rev-parse", "--show-toplevel")
    return Path(top).resolve()


def _normalize_files(files: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in files:
        value = str(raw).strip().replace("\\", "/")
        path = Path(value)
        if not value or path.is_absolute() or value == "." or value.startswith("../") or "/../" in value:
            raise ValueError(f"declared file must be a relative path without traversal: {raw!r}")
        normalized.append(value.lstrip("./"))
    return tuple(dict.fromkeys(normalized))


@dataclass(frozen=True)
class ParallelTaskSpec:
    repo_root: str
    base_commit: str
    declared_files: tuple[str, ...]
    dependency_keys: tuple[str, ...] = ()
    owner: str = "edge-agent"
    task_id: str = field(default_factory=lambda: f"parallel-{uuid.uuid4().hex}")
    worktree_root: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_root", str(_canonical_repo(self.repo_root)))
        object.__setattr__(self, "declared_files", _normalize_files(self.declared_files))
        object.__setattr__(self, "dependency_keys", tuple(dict.fromkeys(str(x).strip() for x in self.dependency_keys if str(x).strip())))
        if not self.task_id or "/" in self.task_id or ".." in self.task_id:
            raise ValueError("task_id must be a non-empty safe identifier")
        if not self.base_commit.strip():
            raise ValueError("base_commit is required; dirty source snapshots are not implicit")
        if not self.declared_files:
            raise ValueError("parallel tasks require declared_files; unknown scope is serial-only")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WorktreeManifest:
    schema: str
    task_id: str
    repo_root: str
    base_commit: str
    worktree_path: str
    manifest_path: str
    owner: str
    declared_files: list[str]
    dependency_keys: list[str]
    state: str
    created_at: str
    updated_at: str
    result: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class SourceDirtyError(RuntimeError):
    pass


class WorktreeManager:
    """Create and inspect isolated worktrees; integration belongs to P4."""

    def __init__(self, *, state_root: str | Path | None = None, worktree_root: str | Path | None = None):
        root = Path(state_root or Path.home() / ".edge-agent" / "parallel").expanduser()
        self.state_root = root.resolve()
        self.manifest_root = self.state_root / "manifests"
        self.worktree_root = Path(worktree_root or Path.home() / ".edge-agent-worktrees" / "parallel").expanduser().resolve()

    def _manifest_path(self, task_id: str) -> Path:
        return self.manifest_root / f"{task_id}.json"

    def _worktree_path(self, spec: ParallelTaskSpec) -> Path:
        repo_key = hashlib.sha256(spec.repo_root.encode()).hexdigest()[:16]
        return self.worktree_root / repo_key / spec.task_id

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def create(self, spec: ParallelTaskSpec) -> WorktreeManifest:
        repo = Path(spec.repo_root)
        if _git(repo, "status", "--porcelain", check=False):
            raise SourceDirtyError("source checkout is dirty; provide a clean committed base before creating a parallel worktree")
        _git(repo, "rev-parse", "--verify", f"{spec.base_commit}^{{commit}}")
        resolved_base = _git(repo, "rev-parse", spec.base_commit)
        target = self._worktree_path(spec)
        manifest_path = self._manifest_path(spec.task_id)
        if target.exists() or manifest_path.exists():
            raise FileExistsError(f"parallel task already exists: {spec.task_id}")

        from edge_agent_parallel_locks import repository_lifecycle_lock

        target.parent.mkdir(parents=True, exist_ok=True)
        with repository_lifecycle_lock(repo, state_root=self.state_root):
            if target.exists() or manifest_path.exists():
                raise FileExistsError(f"parallel task already exists: {spec.task_id}")
            # Recheck under the lifecycle lock so a concurrent local edit cannot
            # appear between the initial safety check and `worktree add`.
            if _git(repo, "status", "--porcelain", check=False):
                raise SourceDirtyError("source checkout became dirty during worktree creation")
            result = subprocess.run(
                ["/usr/bin/git", "-C", str(repo), "worktree", "add", "--detach", str(target), resolved_base],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "git worktree add failed").strip()
                raise RuntimeError(detail[-500:])
            timestamp = _now()
            manifest = WorktreeManifest(
                schema=SCHEMA,
                task_id=spec.task_id,
                repo_root=str(repo),
                base_commit=resolved_base,
                worktree_path=str(target),
                manifest_path=str(manifest_path),
                owner=spec.owner,
                declared_files=list(spec.declared_files),
                dependency_keys=list(spec.dependency_keys),
                state="created",
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._write_json(manifest_path, manifest.to_dict())
            return manifest

    def read(self, task_id: str) -> WorktreeManifest:
        path = self._manifest_path(task_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != SCHEMA:
            raise ValueError("unsupported parallel worktree manifest schema")
        return WorktreeManifest(**payload)

    def update(self, task_id: str, *, state: str | None = None, result: dict | None = None) -> WorktreeManifest:
        manifest = self.read(task_id)
        if state is not None:
            if state not in ACTIVE_STATES | TERMINAL_STATES:
                raise ValueError(f"invalid worktree state: {state}")
            if manifest.state in TERMINAL_STATES and state != manifest.state:
                raise ValueError(f"terminal task cannot transition from {manifest.state} to {state}")
            manifest.state = state
        if result is not None:
            manifest.result = dict(result)
        manifest.updated_at = _now()
        self._write_json(Path(manifest.manifest_path), manifest.to_dict())
        return manifest

    def remove_clean(self, task_id: str) -> None:
        manifest = self.read(task_id)
        if manifest.state not in TERMINAL_STATES:
            raise RuntimeError("only terminal tasks may remove a worktree")
        worktree = Path(manifest.worktree_path)
        if not worktree.exists():
            return
        if _git(worktree, "status", "--porcelain", check=False):
            raise RuntimeError("worktree has uncommitted changes; preserve it for review instead of deleting")
        from edge_agent_parallel_locks import repository_lifecycle_lock

        with repository_lifecycle_lock(manifest.repo_root, state_root=self.state_root):
            result = subprocess.run(
                ["/usr/bin/git", "-C", manifest.repo_root, "worktree", "remove", str(worktree)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "git worktree remove failed").strip()
                raise RuntimeError(detail[-500:])
