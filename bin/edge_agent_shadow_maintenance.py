"""Offline/operational maintenance primitives for a Shadow root.

This module never selects an operating root implicitly.  Callers must inject a
``ShadowEventStore`` whose root has already passed the Observer boundary
checks.  All destructive operations are dry-run by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Any, Callable, Iterator

from edge_agent_shadow_event_store import ShadowEventStore, ShadowEventStoreBusy


class ShadowMaintenanceError(RuntimeError):
    """Maintenance could not safely proceed."""


NORMAL = "NORMAL"
SOFT_LIMIT = "SOFT_LIMIT"
HARD_LIMIT = "HARD_LIMIT"
READ_ONLY_DEGRADED = "READ_ONLY_DEGRADED"
RECOVERING = "RECOVERING"


@dataclass(frozen=True)
class ShadowMaintenanceConfig:
    retention_days: int = 30
    jsonl_retention_days: int = 14
    sqlite_max_bytes: int = 512 * 1024 * 1024
    jsonl_segment_max_bytes: int = 256 * 1024 * 1024
    total_soft_limit_bytes: int = 768 * 1024 * 1024
    total_hard_limit_bytes: int = 1024 * 1024 * 1024
    retention_batch_size: int = 500
    maintenance_interval_seconds: float = 3600.0
    jsonl_rotation_interval_seconds: float = 86400.0

    def __post_init__(self) -> None:
        if self.retention_days <= 0 or self.jsonl_retention_days <= 0:
            raise ValueError("retention days must be positive")
        if self.sqlite_max_bytes <= 0 or self.jsonl_segment_max_bytes <= 0:
            raise ValueError("file limits must be positive")
        if not (0 < self.total_soft_limit_bytes < self.total_hard_limit_bytes):
            raise ValueError("soft limit must be below hard limit")
        if self.retention_batch_size <= 0 or self.maintenance_interval_seconds <= 0 or self.jsonl_rotation_interval_seconds <= 0:
            raise ValueError("maintenance settings must be positive")


@dataclass(frozen=True)
class ShadowCanaryConfig:
    """Offline validation contract for the future single-Bot canary."""

    provider_role: str = "antigravity"
    enabled: bool = False
    root: Path | None = None
    key_path: Path | None = None
    other_provider_flags_off: bool = True
    central_claim_enabled: bool = False
    telegram_output_enabled: bool = False
    sqlite_max_bytes: int = 512 * 1024 * 1024
    jsonl_segment_max_bytes: int = 256 * 1024 * 1024

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.provider_role.casefold() != "antigravity":
            errors.append("only Antigravity is approved for the first canary")
        if self.enabled and self.root is None:
            errors.append("enabled canary requires an isolated Shadow root")
        if self.enabled and self.key_path is None:
            errors.append("enabled canary requires a dedicated HMAC key path")
        if not self.other_provider_flags_off:
            errors.append("non-canary provider flags must remain OFF")
        if self.central_claim_enabled:
            errors.append("central claim must remain disabled")
        if self.telegram_output_enabled:
            errors.append("Shadow Telegram output must remain disabled")
        if self.sqlite_max_bytes <= 0 or self.jsonl_segment_max_bytes <= 0:
            errors.append("canary size limits must be positive")
        return errors


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat().replace("+00:00", "Z")


class ShadowMaintenance:
    def __init__(
        self,
        store: ShadowEventStore,
        *,
        config: ShadowMaintenanceConfig | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.config = config or ShadowMaintenanceConfig()
        self.clock = clock
        canonical_root = self.store.root.resolve()
        forbidden = {
            Path("/"),
            Path.home().resolve(),
            (Path.home() / ".edge-agent" / "state").resolve(),
        }
        if canonical_root in forbidden:
            raise ShadowMaintenanceError("maintenance root is outside the allowed Shadow scope")
        self.lock_path = self.store.root / "maintenance.lock"
        self.health_path = self.store.root / "health.yaml"

    def _assert_root(self) -> None:
        info = self.store.root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ShadowMaintenanceError("Shadow root must be a real directory")
        if info.st_uid != os.geteuid():
            raise ShadowMaintenanceError("Shadow root owner mismatch")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ShadowMaintenanceError("Shadow root permissions are too broad")

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self._assert_root()
        descriptor = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ShadowMaintenanceError("maintenance lock is busy") from exc
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def disk_usage(self) -> dict[str, int]:
        self._assert_root()
        sqlite_bytes = self.store.sqlite_storage_bytes()
        jsonl_bytes = sum(path.stat().st_size for path in self.store.jsonl_paths() if path.exists())
        total = 0
        for path in self.store.root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
        return {"sqlite_bytes": sqlite_bytes, "jsonl_bytes": jsonl_bytes, "total_bytes": total}

    def disk_state(self) -> str:
        usage = self.disk_usage()
        if (
            usage["sqlite_bytes"] >= self.config.sqlite_max_bytes
            or usage["total_bytes"] >= self.config.total_hard_limit_bytes
        ):
            return HARD_LIMIT
        if (
            usage["jsonl_bytes"] >= self.config.jsonl_segment_max_bytes
            or usage["total_bytes"] >= self.config.total_soft_limit_bytes
        ):
            return SOFT_LIMIT
        return NORMAL

    def retention_dry_run(self, *, now: float | None = None) -> dict[str, Any]:
        timestamp = self.clock() if now is None else now
        cutoff = _utc(timestamp - self.config.retention_days * 86400)
        return self.store.retention_dry_run(cutoff=cutoff, batch_size=self.config.retention_batch_size)

    def retention_execute(self, *, now: float | None = None) -> dict[str, Any]:
        with self._lock():
            timestamp = self.clock() if now is None else now
            cutoff = _utc(timestamp - self.config.retention_days * 86400)
            return self.store.retention_execute(cutoff=cutoff, batch_size=self.config.retention_batch_size)

    def jsonl_retention_dry_run(self, *, now: float | None = None) -> dict[str, Any]:
        timestamp = self.clock() if now is None else now
        cutoff = timestamp - self.config.jsonl_retention_days * 86400
        candidates = [
            path for path in self.store.closed_jsonl_paths()
            if not path.is_symlink() and path.stat().st_mtime < cutoff
        ]
        return {
            "cutoff": _utc(cutoff),
            "candidate_count": len(candidates),
            "estimated_bytes": sum(path.stat().st_size for path in candidates),
            "segments": [path.name for path in candidates],
        }

    def jsonl_retention_execute(self, *, now: float | None = None) -> dict[str, Any]:
        with self._lock():
            preview = self.jsonl_retention_dry_run(now=now)
            deleted: list[str] = []
            for name in preview["segments"]:
                path = self.store.root / name
                if path.is_symlink() or path == self.store.event_log_path:
                    raise ShadowMaintenanceError("unsafe JSONL retention target")
                if path.exists() and path.is_file():
                    path.unlink()
                    deleted.append(name)
            preview["deleted_segments"] = deleted
            return preview

    def rotate_if_needed(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock():
            if not force and self.store.event_log_path.exists():
                age = self.clock() - self.store.event_log_path.stat().st_mtime
                force = age >= self.config.jsonl_rotation_interval_seconds
            return self.store.rotate_jsonl(
                max_bytes=self.config.jsonl_segment_max_bytes,
                now=self.clock(),
                force=force,
            )

    def enforce_soft_limit(self) -> dict[str, Any]:
        state = self.disk_state()
        result: dict[str, Any] = {"state_before": state, "retention": None, "rotation": None}
        if state == SOFT_LIMIT:
            result["retention"] = self.retention_execute()
            result["rotation"] = self.rotate_if_needed()
            result["jsonl_retention"] = self.jsonl_retention_execute()
            result["state_after"] = self.disk_state()
        else:
            result["state_after"] = state
        return result

    def recover(self) -> dict[str, Any]:
        with self._lock():
            removed = self.store.recover_pending_outbox()
            quarantined = self.store.quarantine_corrupt_segments()
            return {"flushing_requeued": removed, "quarantined_segments": quarantined}

    def health_snapshot(self, *, observer_stats: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            state = self.disk_state()
            snapshot = self.store.health_snapshot()
        except (OSError, ShadowEventStoreBusy, ShadowMaintenanceError):
            state = READ_ONLY_DEGRADED
            snapshot = {"sqlite_bytes": 0, "jsonl_bytes": 0, "segment_count": 0}
        snapshot.update({"enabled": True, "disk_state": state})
        snapshot.setdefault("last_maintenance_at", None)
        snapshot.setdefault("last_rotation_at", None)
        snapshot.setdefault("active_key_id", None)
        if observer_stats:
            for key in (
                "queue_depth", "queue_high_watermark", "processed", "dropped_queue_full",
                    "dropped_disk_budget", "failed_store", "worker_alive_after_stop",
                    "active_key_id",
            ):
                if key in observer_stats:
                    snapshot[key] = observer_stats[key]
        return snapshot

    def write_health_snapshot(self, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        data = snapshot or self.health_snapshot()
        lines = [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in sorted(data.items())]
        fd, temporary = tempfile.mkstemp(prefix=".health.", dir=self.store.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.health_path)
            os.chmod(self.health_path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return data

    def purge_all(self, *, dry_run: bool = True, feature_enabled: bool = False, observer_active: bool = False) -> dict[str, Any]:
        self._assert_root()
        if not dry_run and (feature_enabled or observer_active):
            raise ShadowMaintenanceError("purge requires Shadow OFF and Observer stopped")
        candidates = []
        for path in self.store.root.iterdir():
            if path.is_symlink() or path == self.lock_path:
                if path.is_symlink():
                    raise ShadowMaintenanceError("symlink purge target is forbidden")
                continue
            if path.is_file():
                candidates.append({"name": path.name, "bytes": path.stat().st_size})
            elif path.is_dir():
                raise ShadowMaintenanceError("unexpected nested purge directory")
        result = {"dry_run": dry_run, "files": candidates, "bytes": sum(item["bytes"] for item in candidates)}
        if dry_run:
            return result
        with self._lock():
            for item in candidates:
                target = self.store.root / item["name"]
                if target.exists() and not target.is_symlink():
                    target.unlink()
            result["deleted"] = len(candidates)
            return result

    def command(self, name: str, *, execute: bool = False, feature_enabled: bool = False,
                observer_active: bool = False) -> dict[str, Any]:
        """Small offline API corresponding to the documented maintenance commands."""

        if name == "status" or name == "verify":
            return self.health_snapshot()
        if name == "retention-dry-run":
            return self.retention_dry_run()
        if name == "retention-execute":
            if not execute:
                return self.retention_dry_run()
            return self.retention_execute()
        if name == "purge-all-dry-run":
            return self.purge_all(dry_run=True, feature_enabled=feature_enabled, observer_active=observer_active)
        if name == "purge-all-execute":
            return self.purge_all(dry_run=False, feature_enabled=feature_enabled, observer_active=observer_active)
        raise ShadowMaintenanceError(f"unknown maintenance command: {name}")


__all__ = [
    "HARD_LIMIT", "NORMAL", "READ_ONLY_DEGRADED", "RECOVERING", "SOFT_LIMIT",
    "ShadowMaintenance", "ShadowMaintenanceConfig", "ShadowMaintenanceError",
    "ShadowCanaryConfig",
]
