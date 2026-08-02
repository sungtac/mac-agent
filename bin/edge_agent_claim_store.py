"""SQLite-backed claim lease and fencing primitives for Shadow Mode.

The store is intentionally path-injected.  A caller must provide a test or
shadow path; no operational state directory is selected implicitly.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Callable, Any

from edge_agent_provenance import sign_provenance, verify_provenance
from edge_agent_secure_paths import ensure_private_directory, reject_symlink_components


SCHEMA_VERSION = "edge_agent.claim_store.v1"
CLAIMED = "CLAIMED"
ACTIVE = "ACTIVE"
EXPIRED = "EXPIRED"
RECOVERY_CANDIDATE = "RECOVERY_CANDIDATE"
COMPLETED = "COMPLETED"
FENCING_REJECTED = "FENCING_REJECTED"


class ClaimStoreError(RuntimeError):
    """Base error for claim-store failures."""


class ClaimStoreBusy(ClaimStoreError):
    """Raised when SQLite remains locked after the configured timeout."""


@dataclass(frozen=True)
class ClaimResult:
    acquired: bool
    duplicate: bool
    status: str
    message_key: str
    root_task_id: str
    claim_owner: str | None
    fencing_token: int | None
    reason: str | None = None


@dataclass(frozen=True)
class ClaimRecord:
    message_key: str
    root_task_id: str
    claim_owner: str
    fencing_token: int
    claimed_at: float
    lease_expires_at: float
    last_heartbeat_at: float
    status: str


class ClaimStore:
    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
        busy_timeout_ms: int = 1000,
        max_clock_jump_seconds: float | None = None,
        allowed_owners: set[str] | frozenset[str] | None = None,
        provenance_key_path: str | Path | None = None,
    ) -> None:
        if ttl_seconds <= 0 or not math.isfinite(ttl_seconds):
            raise ValueError("ttl_seconds must be a positive finite number")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        self.path = reject_symlink_components(Path(path).expanduser())
        ensure_private_directory(self.path.parent)
        self.ttl_seconds = float(ttl_seconds)
        self.clock = clock
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.allowed_owners = frozenset(str(item) for item in allowed_owners) if allowed_owners is not None else None
        self.provenance_key_path = provenance_key_path
        self.max_clock_jump_seconds = (
            float(max_clock_jump_seconds)
            if max_clock_jump_seconds is not None
            else max(300.0, self.ttl_seconds * 100.0)
        )
        self._schema_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextlib.contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._schema_lock, self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS claim_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims (
                    message_key TEXT PRIMARY KEY,
                    root_task_id TEXT NOT NULL,
                    claim_owner TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    claimed_at REAL NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    last_heartbeat_at REAL NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
                CREATE TABLE IF NOT EXISTS claim_provenance_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO claim_meta(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )

    def _append_provenance(self, connection: sqlite3.Connection, event_type: str, *, message_key: str, root_task_id: str, claim_owner: str, fencing_token: int, status: str, reason: str = "", observed_at: float | None = None) -> None:
        payload = {
            "message_key": str(message_key),
            "root_task_id": str(root_task_id),
            "claim_owner": str(claim_owner),
            "fencing_token": int(fencing_token),
            "status": str(status),
            "reason": str(reason),
            "observed_at": float(time.time() if observed_at is None else observed_at),
        }
        key_id, signature = sign_provenance("claim_lease", {"event_type": event_type, **payload}, key_path=self.provenance_key_path)
        if signature:
            connection.execute(
                "INSERT INTO claim_provenance_events(event_id, event_type, payload, key_id, signature, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (f"claim-event-{uuid.uuid4().hex}", event_type, json.dumps(payload, ensure_ascii=False, sort_keys=True), key_id, signature, time.time()),
            )

    def provenance_events(self, message_key: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT event_type, payload, key_id, signature, created_at FROM claim_provenance_events"
        args: tuple[Any, ...] = ()
        if message_key is not None:
            query += " WHERE json_extract(payload, '$.message_key') = ?"
            args = (str(message_key),)
        query += " ORDER BY created_at, rowid"
        with self._connection() as connection:
            rows = connection.execute(query, args).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload"])
            verify_provenance("claim_lease", {"event_type": row["event_type"], **payload}, key_id=row["key_id"], signature=row["signature"], key_path=self.provenance_key_path)
            result.append({"event_type": row["event_type"], **payload, "key_id": row["key_id"], "signature": row["signature"], "created_at": float(row["created_at"])})
        return result

    @staticmethod
    def _now(clock: Callable[[], float], explicit: float | None) -> float:
        value = float(clock() if explicit is None else explicit)
        if not math.isfinite(value):
            raise ValueError("clock value must be finite")
        return value

    def _clock_uncertain(self, row: sqlite3.Row, now: float) -> bool:
        previous = float(row["last_heartbeat_at"])
        return now < previous or now - previous > self.max_clock_jump_seconds

    @staticmethod
    def _row_to_record(row: sqlite3.Row | None) -> ClaimRecord | None:
        if row is None:
            return None
        return ClaimRecord(
            message_key=row["message_key"],
            root_task_id=row["root_task_id"],
            claim_owner=row["claim_owner"],
            fencing_token=int(row["fencing_token"]),
            claimed_at=float(row["claimed_at"]),
            lease_expires_at=float(row["lease_expires_at"]),
            last_heartbeat_at=float(row["last_heartbeat_at"]),
            status=row["status"],
        )

    def _busy(self, exc: sqlite3.OperationalError) -> ClaimStoreBusy:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return ClaimStoreBusy(f"claim store busy: {self.path}")
        raise exc

    def get(self, message_key: str) -> ClaimRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM claims WHERE message_key = ?", (str(message_key),)
            ).fetchone()
        return self._row_to_record(row)

    def claim(
        self,
        message_key: str,
        root_task_id: str,
        claim_owner: str,
        *,
        now: float | None = None,
    ) -> ClaimResult:
        message_key = str(message_key)
        root_task_id = str(root_task_id)
        claim_owner = str(claim_owner)
        if not message_key or not root_task_id or not claim_owner:
            raise ValueError("message_key, root_task_id, and claim_owner are required")
        if self.allowed_owners is not None and claim_owner not in self.allowed_owners:
            raise ClaimStoreError("claim owner is not authorized for this store")
        current = self._now(self.clock, now)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM claims WHERE message_key = ?", (message_key,)
                ).fetchone()
                if row is None:
                    connection.execute(
                        """INSERT INTO claims(
                            message_key, root_task_id, claim_owner, fencing_token,
                            claimed_at, lease_expires_at, last_heartbeat_at, status
                        ) VALUES(?, ?, ?, 1, ?, ?, ?, ?)""",
                        (message_key, root_task_id, claim_owner, current, current + self.ttl_seconds, current, CLAIMED),
                    )
                    self._append_provenance(connection, "claim_acquired", message_key=message_key, root_task_id=root_task_id, claim_owner=claim_owner, fencing_token=1, status=CLAIMED, observed_at=current)
                    connection.commit()
                    return ClaimResult(True, False, CLAIMED, message_key, root_task_id, claim_owner, 1)

                existing = self._row_to_record(row)
                assert existing is not None
                if self._clock_uncertain(row, current):
                    connection.execute(
                        "UPDATE claims SET status = ? WHERE message_key = ?",
                        (RECOVERY_CANDIDATE, message_key),
                    )
                    self._append_provenance(connection, "recovery_candidate", message_key=message_key, root_task_id=existing.root_task_id, claim_owner=existing.claim_owner, fencing_token=existing.fencing_token, status=RECOVERY_CANDIDATE, reason="clock_uncertain", observed_at=current)
                    connection.commit()
                    return ClaimResult(False, False, RECOVERY_CANDIDATE, message_key, existing.root_task_id, existing.claim_owner, existing.fencing_token, "clock_uncertain")

                if existing.status == COMPLETED:
                    connection.commit()
                    return ClaimResult(False, True, COMPLETED, message_key, existing.root_task_id, existing.claim_owner, existing.fencing_token, "already_completed")

                if current < existing.lease_expires_at:
                    connection.commit()
                    if existing.claim_owner == claim_owner and existing.root_task_id == root_task_id:
                        return ClaimResult(True, True, existing.status, message_key, root_task_id, claim_owner, existing.fencing_token, "idempotent_owner")
                    return ClaimResult(False, True, ACTIVE, message_key, existing.root_task_id, existing.claim_owner, existing.fencing_token, "lease_active")

                next_token = existing.fencing_token + 1
                connection.execute(
                    """UPDATE claims SET root_task_id = ?, claim_owner = ?, fencing_token = ?,
                       claimed_at = ?, lease_expires_at = ?, last_heartbeat_at = ?, status = ?
                       WHERE message_key = ?""",
                    (root_task_id, claim_owner, next_token, current, current + self.ttl_seconds, current, CLAIMED, message_key),
                )
                self._append_provenance(connection, "claim_reacquired", message_key=message_key, root_task_id=root_task_id, claim_owner=claim_owner, fencing_token=next_token, status=CLAIMED, reason="lease_expired", observed_at=current)
                connection.commit()
                return ClaimResult(True, False, CLAIMED, message_key, root_task_id, claim_owner, next_token, "lease_expired")
        except sqlite3.OperationalError as exc:
            raise self._busy(exc)

    def heartbeat(
        self,
        message_key: str,
        claim_owner: str,
        fencing_token: int,
        *,
        now: float | None = None,
    ) -> ClaimResult:
        current = self._now(self.clock, now)
        message_key = str(message_key)
        claim_owner = str(claim_owner)
        if self.allowed_owners is not None and claim_owner not in self.allowed_owners:
            raise ClaimStoreError("claim owner is not authorized for this store")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM claims WHERE message_key = ?", (message_key,)).fetchone()
            existing = self._row_to_record(row)
            if existing is None:
                connection.commit()
                return ClaimResult(False, False, FENCING_REJECTED, message_key, "", None, None, "claim_missing")
            if existing.claim_owner != claim_owner or existing.fencing_token != fencing_token:
                connection.commit()
                return ClaimResult(False, False, FENCING_REJECTED, message_key, existing.root_task_id, existing.claim_owner, existing.fencing_token, "fencing_token_mismatch")
            if self._clock_uncertain(row, current):
                connection.execute("UPDATE claims SET status = ? WHERE message_key = ?", (RECOVERY_CANDIDATE, message_key))
                self._append_provenance(connection, "recovery_candidate", message_key=message_key, root_task_id=existing.root_task_id, claim_owner=existing.claim_owner, fencing_token=existing.fencing_token, status=RECOVERY_CANDIDATE, reason="clock_uncertain", observed_at=current)
                connection.commit()
                return ClaimResult(False, False, RECOVERY_CANDIDATE, message_key, existing.root_task_id, existing.claim_owner, existing.fencing_token, "clock_uncertain")
            if current >= existing.lease_expires_at:
                connection.execute("UPDATE claims SET status = ? WHERE message_key = ?", (EXPIRED, message_key))
                self._append_provenance(connection, "lease_expired", message_key=message_key, root_task_id=existing.root_task_id, claim_owner=existing.claim_owner, fencing_token=existing.fencing_token, status=EXPIRED, reason="lease_expired", observed_at=current)
                connection.commit()
                return ClaimResult(False, False, EXPIRED, message_key, existing.root_task_id, existing.claim_owner, existing.fencing_token, "lease_expired")
            connection.execute(
                "UPDATE claims SET last_heartbeat_at = ?, lease_expires_at = ?, status = ? WHERE message_key = ?",
                (current, current + self.ttl_seconds, ACTIVE, message_key),
            )
            self._append_provenance(connection, "heartbeat", message_key=message_key, root_task_id=existing.root_task_id, claim_owner=claim_owner, fencing_token=fencing_token, status=ACTIVE, observed_at=current)
            connection.commit()
            return ClaimResult(True, False, ACTIVE, message_key, existing.root_task_id, claim_owner, fencing_token)

    def complete(self, message_key: str, claim_owner: str, fencing_token: int) -> ClaimResult:
        message_key = str(message_key)
        if self.allowed_owners is not None and str(claim_owner) not in self.allowed_owners:
            raise ClaimStoreError("claim owner is not authorized for this store")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM claims WHERE message_key = ?", (message_key,)).fetchone()
            existing = self._row_to_record(row)
            if existing is None or existing.claim_owner != str(claim_owner) or existing.fencing_token != fencing_token:
                connection.commit()
                return ClaimResult(False, False, FENCING_REJECTED, message_key, existing.root_task_id if existing else "", existing.claim_owner if existing else None, existing.fencing_token if existing else None, "fencing_token_mismatch")
            connection.execute("UPDATE claims SET status = ? WHERE message_key = ?", (COMPLETED, message_key))
            self._append_provenance(connection, "completed", message_key=message_key, root_task_id=existing.root_task_id, claim_owner=existing.claim_owner, fencing_token=existing.fencing_token, status=COMPLETED)
            connection.commit()
            return ClaimResult(True, False, COMPLETED, message_key, existing.root_task_id, existing.claim_owner, existing.fencing_token)

    def validate_fencing_token(self, message_key: str, fencing_token: int) -> bool:
        record = self.get(message_key)
        return bool(record and record.fencing_token == fencing_token and record.status in {CLAIMED, ACTIVE})
