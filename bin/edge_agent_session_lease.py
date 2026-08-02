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

from edge_agent_provenance import sign_provenance, verify_provenance
from edge_agent_session_contract import _check_safe_text, _require_id
from edge_agent_secure_paths import ensure_private_directory, open_lock, read_text


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
    key_id: str = ""
    signature: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class SessionLeaseManager:
    """OS-backed lease; stale metadata never blocks a new flock owner."""

    def __init__(self, root: str | Path | None = None, *, provenance_key_path: str | Path | None = None):
        self.root = ensure_private_directory(Path(root or Path.home() / ".edge-agent" / "sessions" / "leases").expanduser())
        self.provenance_key_path = provenance_key_path

    def _paths(self, session_id: str) -> tuple[Path, Path]:
        safe = _require_id("logical_session_id", session_id)
        return self.root / f"{safe}.lock", self.root / f"{safe}.json"

    @staticmethod
    def _write_metadata(path: Path, payload: dict) -> None:
        ensure_private_directory(path.parent)
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
        descriptor = open_lock(lock_path)
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
            active_payload = lease.to_dict()
            active_payload["key_id"] = os.environ.get("EDGE_AGENT_MESSAGE_KEY_ID", "agent-message-v1").strip() or "agent-message-v1"
            key_id, signature = sign_provenance("session_lease", active_payload, key_path=self.provenance_key_path, key_id=active_payload["key_id"])
            lease = SessionLease(**{**active_payload, "key_id": key_id, "signature": signature})
            self._write_metadata(metadata_path, lease.to_dict())
            yield lease
        finally:
            if lease is not None:
                released = lease.to_dict()
                released["state"] = "released"
                released["released_at"] = _iso(_now())
                released["signature"] = ""
                released["key_id"], released["signature"] = sign_provenance("session_lease", released, key_path=self.provenance_key_path, key_id=str(released.get("key_id", "")))
                self._write_metadata(metadata_path, released)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def current_metadata(self, session_id: str) -> dict | None:
        _, metadata_path = self._paths(session_id)
        try:
            payload = json.loads(read_text(metadata_path))
        except (FileNotFoundError, OSError, RuntimeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("signature"):
            signature = str(payload.pop("signature"))
            try:
                verify_provenance("session_lease", payload, key_id=str(payload.get("key_id", "")), signature=signature, key_path=self.provenance_key_path)
            finally:
                payload["signature"] = signature
        return payload
