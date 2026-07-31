#!/usr/bin/env python3
"""Cross-process lease for one logical session's active provider execution."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from edge_agent_session_contract import _check_safe_text, _require_id


class SessionLeaseBusy(RuntimeError):
    """Another channel/provider currently owns this logical session."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


@dataclass(frozen=True)
class SessionLease:
    schema: str
    lease_id: str
    logical_session_id: str
    owner: str
    acquired_at: str
    expires_at: str
    state: str = "active"

    def to_dict(self) -> dict:
        return asdict(self)


class SessionLeaseManager:
    """OS-backed lease; stale metadata never blocks a new flock owner."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or Path.home() / ".edge-agent" / "sessions" / "leases").expanduser().resolve()

    def _paths(self, session_id: str) -> tuple[Path, Path]:
        safe = _require_id("logical_session_id", session_id)
        return self.root / f"{safe}.lock", self.root / f"{safe}.json"

    @staticmethod
    def _write_metadata(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
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

    @contextlib.contextmanager
    def acquire(self, session_id: str, owner: str, *, ttl_seconds: int = 1800) -> Iterator[SessionLease]:
        session_id = _require_id("logical_session_id", session_id)
        owner = _check_safe_text("owner", owner).strip()
        if not owner:
            raise ValueError("owner is required")
        if ttl_seconds < 1 or ttl_seconds > 24 * 60 * 60:
            raise ValueError("ttl_seconds must be between 1 and 86400")

        lock_path, metadata_path = self._paths(session_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        lease: SessionLease | None = None
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SessionLeaseBusy(f"logical session is already leased: {session_id}") from exc

            acquired = _now()
            lease = SessionLease(
                schema="edge_agent.session_lease.v1",
                lease_id=f"lease-{uuid.uuid4().hex[:16]}",
                logical_session_id=session_id,
                owner=owner,
                acquired_at=_iso(acquired),
                expires_at=_iso(acquired + timedelta(seconds=ttl_seconds)),
            )
            self._write_metadata(metadata_path, lease.to_dict())
            yield lease
        finally:
            if lease is not None:
                released = lease.to_dict()
                released["state"] = "released"
                released["released_at"] = _iso(_now())
                self._write_metadata(metadata_path, released)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def current_metadata(self, session_id: str) -> dict | None:
        _, metadata_path = self._paths(session_id)
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None
