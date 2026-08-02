"""Pipeline P2: lifecycle locks and file/dependency reservations.

The repository lifecycle and integration locks are intentionally separate from
per-task locks. A repository-wide execution lock is not used here, otherwise
different non-overlapping worktrees could never run in parallel.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from edge_agent_locks import canonical_repository_root
from edge_agent_secure_paths import ensure_private_directory, open_lock, read_text


SHARED_REPO_LOCK_DIR = Path.home() / ".claude" / "discord-bot" / "repo-locks"


class ParallelLockBusy(RuntimeError):
    pass


class ReservationConflict(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo_key(repo_root: str | Path) -> str:
    canonical = str(canonical_repository_root(repo_root))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _state_root(state_root: str | Path | None) -> Path:
    return ensure_private_directory(Path(state_root or Path.home() / ".edge-agent" / "parallel").expanduser())


@contextlib.contextmanager
def _lock(path: Path, *, blocking: bool) -> Iterator[None]:
    descriptor = open_lock(path)
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, flags)
        except BlockingIOError as exc:
            raise ParallelLockBusy(str(path)) from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def repository_lifecycle_lock(repo_root: str | Path, *, state_root: str | Path | None = None) -> Iterator[None]:
    base = SHARED_REPO_LOCK_DIR if state_root is None else _state_root(state_root) / "locks"
    path = base / f"{_repo_key(repo_root)}.lock"
    return _lock(path, blocking=False)


def integration_lock(repo_root: str | Path, *, state_root: str | Path | None = None) -> Iterator[None]:
    path = _state_root(state_root) / "locks" / f"integration-{_repo_key(repo_root)}.lock"
    return _lock(path, blocking=False)


def task_lock(task_id: str, *, state_root: str | Path | None = None) -> Iterator[None]:
    safe = str(task_id).replace("/", "_")
    path = _state_root(state_root) / "locks" / f"task-{safe}.lock"
    return _lock(path, blocking=False)


def _normalize(value: str) -> str:
    return str(value).strip().replace("\\", "/").lstrip("./")


def reservation_age_seconds(record: dict, *, now: datetime | None = None) -> float | None:
    """Return age from the latest heartbeat, or None for invalid timestamps."""
    timestamp = record.get("heartbeat_at") or record.get("created_at")
    if not timestamp:
        return None
    try:
        observed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - observed.astimezone(timezone.utc)).total_seconds())


def reservation_is_stale(record: dict, *, ttl_seconds: float, now: datetime | None = None) -> bool:
    age = reservation_age_seconds(record, now=now)
    return age is None or age >= max(0.0, float(ttl_seconds))


def _paths_overlap(left: str, right: str) -> bool:
    a, b = _normalize(left), _normalize(right)
    return a == b or a.startswith(f"{b}/") or b.startswith(f"{a}/")


class FileReservation:
    """Atomic reservation registry for declared files and dependency keys."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        state_root: str | Path | None = None,
        ttl_seconds: float = 3600.0,
    ):
        if ttl_seconds <= 0 or not math.isfinite(ttl_seconds):
            raise ValueError("ttl_seconds must be a positive finite number")
        self.repo_root = canonical_repository_root(repo_root)
        self.state_root = _state_root(state_root)
        self.ttl_seconds = float(ttl_seconds)
        ensure_private_directory(self.state_root / "reservations")
        ensure_private_directory(self.state_root / "locks")
        key = _repo_key(self.repo_root)
        self.registry = self.state_root / "reservations" / f"{key}.json"
        self.lock_path = self.state_root / "locks" / f"reservation-{key}.lock"

    def _read(self) -> list[dict]:
        if not self.registry.exists():
            return []
        payload = json.loads(read_text(self.registry))
        if not isinstance(payload, list):
            raise ValueError("reservation registry must be a JSON array")
        return payload

    def _write(self, payload: list[dict]) -> None:
        ensure_private_directory(self.registry.parent)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.registry.name}.", dir=self.registry.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.registry)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def reserve(self, *, task_id: str, files: tuple[str, ...] | list[str], dependency_keys: tuple[str, ...] | list[str] = (), owner: str = "edge-agent") -> dict:
        requested_files = tuple(dict.fromkeys(_normalize(x) for x in files if _normalize(x)))
        requested_dependencies = tuple(dict.fromkeys(_normalize(x) for x in dependency_keys if _normalize(x)))
        if not requested_files:
            raise ReservationConflict("parallel reservation requires declared files")
        # The registry critical section is tiny. Waiting here avoids turning
        # harmless simultaneous reservations for different files into a false
        # task conflict; the conflict decision itself remains non-overlapping.
        with _lock(self.lock_path, blocking=True):
            records = self._read()
            active = []
            for record in records:
                if record.get("state") != "active" or record.get("task_id") == task_id:
                    continue
                if reservation_is_stale(record, ttl_seconds=self.ttl_seconds):
                    record["state"] = "stale"
                    record["stale_at"] = _now()
                    continue
                active.append(record)
            for record in active:
                if any(_paths_overlap(left, right) for left in requested_files for right in record.get("files", [])):
                    raise ReservationConflict(f"file reservation overlaps task {record.get('task_id')}")
                if set(requested_dependencies) & set(record.get("dependency_keys", [])):
                    raise ReservationConflict(f"dependency reservation overlaps task {record.get('task_id')}")
            reservation = {
                "schema": "edge_agent_parallel_reservation.v1",
                "task_id": task_id,
                "repo_root": str(self.repo_root),
                "owner": owner,
                "files": list(requested_files),
                "dependency_keys": list(requested_dependencies),
                "state": "active",
                "created_at": _now(),
                "heartbeat_at": _now(),
            }
            records = [record for record in records if record.get("task_id") != task_id]
            records.append(reservation)
            # Preserve any quarantine transition even when this task has no
            # overlapping files and can be admitted immediately.
            self._write(records)
            return reservation

    def release(self, task_id: str) -> bool:
        with _lock(self.lock_path, blocking=True):
            records = self._read()
            changed = False
            for record in records:
                if record.get("task_id") == task_id and record.get("state") == "active":
                    record["state"] = "released"
                    record["released_at"] = _now()
                    changed = True
            if changed:
                self._write(records)
            return changed

    def heartbeat(self, task_id: str) -> bool:
        """Refresh an active reservation without changing its declared scope."""
        with _lock(self.lock_path, blocking=True):
            records = self._read()
            changed = False
            timestamp = _now()
            for record in records:
                if record.get("task_id") == task_id and record.get("state") == "active":
                    record["heartbeat_at"] = timestamp
                    changed = True
            if changed:
                self._write(records)
            return changed

    def active(self) -> list[dict]:
        with _lock(self.lock_path, blocking=True):
            return [record for record in self._read() if record.get("state") == "active"]
