"""Privacy-minimal SQLite and append-only JSONL store for Shadow Mode.

The root path is always injected by the caller.  Tests use temporary
directories; the module never selects or writes the operational state root.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence


SCHEMA_VERSION = "edge_agent.shadow_event_store.v1"


class ShadowEventError(RuntimeError):
    """Base event-store error."""


class SensitiveDataError(ShadowEventError):
    """Raised when an event contains raw or sensitive data fields."""


class EventConflictError(ShadowEventError):
    """Raised when an existing event ID is reused with different content."""


class ShadowEventStoreBusy(ShadowEventError):
    """Raised when SQLite remains locked after the configured timeout."""


_FORBIDDEN_KEYS = frozenset({
    "body", "body_text", "content", "content_text", "file_path", "filename",
    "message", "message_text", "path", "prompt", "raw_message", "response",
    "secret", "token", "token_path", "attachment_content",
})


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    import hashlib
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _check_sensitive(value: Any, path: str = "event") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).casefold()
            if key_text in _FORBIDDEN_KEYS:
                raise SensitiveDataError(f"raw or sensitive field is not allowed: {path}.{key}")
            _check_sensitive(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_sensitive(child, f"{path}[{index}]")


def _utc_timestamp(value: float) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")


class ShadowEventStore:
    def __init__(
        self,
        root: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        busy_timeout_ms: int = 1000,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        self.root = Path(root).expanduser()
        self._secure_root()
        self.database_path = self.root / "shadow.db"
        self.event_log_path = self.root / "shadow-events.jsonl"
        self.manifest_path = self.root / "manifest.yaml"
        self.rotation_lock_path = self.root / "rotation.lock"
        self.maintenance_lock_path = self.root / "maintenance.lock"
        self.clock = clock
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._manifest_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._connection_count = 0
        self._connection_count_lock = threading.Lock()
        self._validate_managed_paths()
        self._initialize()

    def _validate_managed_file(self, path: Path, *, allow_missing: bool = True) -> bool:
        """Validate one Shadow-owned file without following symlinks."""

        try:
            info = path.lstat()
        except FileNotFoundError:
            if allow_missing:
                return False
            raise ShadowEventError(f"managed Shadow file is missing: {path.name}")
        except OSError as exc:
            raise ShadowEventError(f"managed Shadow file is unavailable: {path.name}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ShadowEventError(f"managed Shadow file must be a regular file: {path.name}")
        if info.st_uid != os.geteuid():
            raise ShadowEventError(f"managed Shadow file owner mismatch: {path.name}")
        os.chmod(path, 0o600)
        return True

    def _validate_managed_paths(self) -> None:
        for path in (
            self.database_path,
            Path(str(self.database_path) + "-wal"),
            Path(str(self.database_path) + "-shm"),
            Path(str(self.database_path) + "-journal"),
            self.event_log_path,
            self.manifest_path,
            self.rotation_lock_path,
            self.maintenance_lock_path,
        ):
            self._validate_managed_file(path)

    def _open_private_lock(self, path: Path) -> int:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(str(path), flags, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise ShadowEventError(f"managed Shadow lock is unsafe: {path.name}")
            os.fchmod(descriptor, 0o600)
            return descriptor
        except ShadowEventError:
            raise
        except OSError as exc:
            raise ShadowEventError(f"managed Shadow lock is unsafe: {path.name}") from exc

    def _secure_root(self) -> None:
        if self.root.exists() or self.root.is_symlink():
            info = self.root.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ShadowEventError("Shadow root must be a real directory")
            if info.st_uid != os.geteuid():
                raise ShadowEventError("Shadow root owner mismatch")
            if stat.S_IMODE(info.st_mode) != 0o700:
                raise ShadowEventError("Shadow root permissions must be exactly 0700")
        else:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if stat.S_IMODE(self.root.stat().st_mode) != 0o700:
                raise ShadowEventError("Shadow root permissions must be exactly 0700")

    def _connect(self) -> sqlite3.Connection:
        self._validate_managed_file(self.database_path)
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
        except BaseException:
            connection.close()
            raise
        with self._connection_count_lock:
            self._connection_count += 1
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()
            with self._connection_count_lock:
                self._connection_count -= 1

    @property
    def active_connection_count(self) -> int:
        with self._connection_count_lock:
            return self._connection_count

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS shadow_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    root_task_id TEXT NOT NULL,
                    revision_id TEXT,
                    run_id TEXT,
                    created_at TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    lifecycle_status TEXT NOT NULL DEFAULT 'terminal'
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_events_root
                    ON shadow_events(root_task_id, created_at);
                CREATE TABLE IF NOT EXISTS jsonl_outbox (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    jsonl_written_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jsonl_outbox_pending
                    ON jsonl_outbox(status, outbox_id);
                """
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(shadow_events)").fetchall()
            }
            if "lifecycle_status" not in columns:
                connection.execute(
                    "ALTER TABLE shadow_events ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'terminal'"
                )
            connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
            connection.execute(
                "INSERT OR REPLACE INTO shadow_meta(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            connection.execute(
                "UPDATE jsonl_outbox SET status = 'PENDING' WHERE status = 'FLUSHING'"
            )
        os.chmod(self.database_path, 0o600)
        self._validate_managed_paths()
        # Initialization and rotation both rename/create the current segment;
        # keep their existence and permission checks under the same stable lock.
        rotation_fd = self._open_private_lock(self.rotation_lock_path)
        fcntl.flock(rotation_fd, fcntl.LOCK_EX)
        try:
            self._validate_managed_file(self.event_log_path)
            with self._manifest_lock:
                if not self.manifest_path.exists():
                    manifest = (
                        "schema_version: " + SCHEMA_VERSION + "\n"
                        "sqlite: shadow.db\n"
                        "jsonl: shadow-events.jsonl\n"
                        "sqlite_journal_mode: WAL\n"
                        "jsonl_authority: derived_outbox\n"
                        "jsonl_current: shadow-events.jsonl\n"
                        "maintenance_schema: shadow-maintenance.v1\n"
                        "stores_raw_content: false\n"
                    ).encode("utf-8")
                    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    try:
                        manifest_fd = os.open(str(self.manifest_path), flags, 0o600)
                    except FileExistsError:
                        self._validate_managed_file(self.manifest_path, allow_missing=False)
                    except OSError as exc:
                        raise ShadowEventError("manifest path is unsafe") from exc
                    else:
                        try:
                            os.write(manifest_fd, manifest)
                            os.fsync(manifest_fd)
                        finally:
                            os.close(manifest_fd)
                self._validate_managed_file(self.manifest_path, allow_missing=False)
        finally:
            fcntl.flock(rotation_fd, fcntl.LOCK_UN)
            os.close(rotation_fd)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Expose a transaction for deterministic rollback tests and tooling."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise ShadowEventStoreBusy(f"shadow store busy: {self.database_path}") from exc
            raise
        finally:
            connection.close()
            with self._connection_count_lock:
                self._connection_count -= 1

    def _normalize_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(event, Mapping):
            raise TypeError("event must be a mapping")
        data = json.loads(_canonical(dict(event)))
        _check_sensitive(data)
        event_type = data.get("event_type")
        root_task_id = data.get("root_task_id")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event_type is required")
        if not isinstance(root_task_id, str) or not root_task_id:
            raise ValueError("root_task_id is required")
        data.setdefault("schema_version", SCHEMA_VERSION)
        data.setdefault("lifecycle_status", str(data.get("status", "terminal")).upper())
        data.setdefault("created_at", _utc_timestamp(self.clock()))
        if not isinstance(data["created_at"], str):
            raise ValueError("created_at must be a string")
        if not data.get("event_id"):
            identity = {key: value for key, value in data.items() if key not in {"event_id", "created_at"}}
            data["event_id"] = _hash(identity)
        if not isinstance(data["event_id"], str) or not data["event_id"]:
            raise ValueError("event_id must be a non-empty string")
        return data

    def _append_jsonl_batch_if_missing(self, data: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        """Append a batch under one file lock and one existing-ID scan."""

        if not data:
            return {"written": 0, "skipped": 0}
        rotation_fd = self._open_private_lock(self.rotation_lock_path)
        fcntl.flock(rotation_fd, fcntl.LOCK_EX)
        try:
            flags = os.O_CREAT | os.O_RDWR | os.O_APPEND
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                event_fd = os.open(str(self.event_log_path), flags, 0o600)
            except OSError as exc:
                raise ShadowEventError("event log path is unsafe") from exc
            with os.fdopen(event_fd, "a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.seek(0)
                    existing_ids: set[str] = set()
                    for line in handle:
                        if not line.strip():
                            continue
                        try:
                            value = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(value, Mapping) and value.get("event_id"):
                            existing_ids.add(str(value["event_id"]))
                    handle.seek(0, 2)
                    written = 0
                    skipped = 0
                    for item in data:
                        event_id = str(item["event_id"])
                        if event_id in existing_ids:
                            skipped += 1
                            continue
                        handle.write(_canonical(item) + "\n")
                        existing_ids.add(event_id)
                        written += 1
                    handle.flush()
                    return {"written": written, "skipped": skipped}
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            fcntl.flock(rotation_fd, fcntl.LOCK_UN)
            os.close(rotation_fd)

    def _append_jsonl_if_missing(self, data: Mapping[str, Any]) -> bool:
        """Backward-compatible single-event helper."""

        result = self._append_jsonl_batch_if_missing([data])
        return result["written"] == 1

    def _append_jsonl(self, data: Mapping[str, Any]) -> None:
        """Backward-compatible direct helper used only by legacy tests/tools."""

        self._append_jsonl_if_missing(data)

    def _mark_outbox_error(self, outbox_id: int, error: BaseException) -> None:
        with self._connection() as connection:
            connection.execute(
                """UPDATE jsonl_outbox
                   SET status = 'PENDING', attempt_count = attempt_count + 1, last_error = ?
                   WHERE outbox_id = ?""",
                (f"{type(error).__name__}: {error}", outbox_id),
            )

    def flush_pending(
        self,
        *,
        limit: int | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, int]:
        """Drain derived JSONL output without changing authoritative SQLite state."""

        started = time.monotonic()
        maximum = None if limit is None else max(0, int(limit))
        with self._flush_lock:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                query = (
                    "SELECT outbox_id, event_id, payload_json FROM jsonl_outbox "
                    "WHERE status = 'PENDING' ORDER BY outbox_id"
                )
                if maximum is not None:
                    query += f" LIMIT {maximum}"
                rows = connection.execute(query).fetchall()
                if rows:
                    connection.executemany(
                        "UPDATE jsonl_outbox SET status = 'FLUSHING' WHERE outbox_id = ?",
                        [(row["outbox_id"],) for row in rows],
                    )
                connection.commit()
            stats = {"processed": 0, "written": 0, "skipped": 0, "errors": 0, "pending": 0}
            if not rows:
                stats["pending"] = self.pending_outbox_count()
                return stats
            if timeout_seconds is not None and time.monotonic() - started >= max(0.0, timeout_seconds):
                for row in rows:
                    self._mark_outbox_error(int(row["outbox_id"]), TimeoutError("flush timeout"))
                stats["errors"] = len(rows)
                stats["pending"] = self.pending_outbox_count()
                return stats
            try:
                payloads = [json.loads(row["payload_json"]) for row in rows]
                if len(payloads) == 1:
                    written = 1 if self._append_jsonl_if_missing(payloads[0]) else 0
                    skipped = 1 - written
                else:
                    result = self._append_jsonl_batch_if_missing(payloads)
                    written = result["written"]
                    skipped = result["skipped"]
                now = _utc_timestamp(self.clock())
                with self._connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.executemany(
                        """UPDATE jsonl_outbox
                           SET status = 'DELIVERED', jsonl_written_at = ?, last_error = NULL
                           WHERE outbox_id = ?""",
                        [(now, row["outbox_id"]) for row in rows],
                    )
                    connection.commit()
                stats["processed"] = len(rows)
                stats["written"] = written
                stats["skipped"] = skipped
            except BaseException as exc:
                stats["errors"] = len(rows)
                for row in rows:
                    try:
                        self._mark_outbox_error(int(row["outbox_id"]), exc)
                    except sqlite3.Error:
                        pass
            stats["pending"] = self.pending_outbox_count()
            return stats

    def pending_outbox_count(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM jsonl_outbox WHERE status = 'PENDING'"
            ).fetchone()
        return int(row["count"])

    def outbox_status(self, event_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT event_id, status, attempt_count, last_error, created_at, jsonl_written_at
                   FROM jsonl_outbox WHERE event_id = ?""",
                (str(event_id),),
            ).fetchone()
        return dict(row) if row else None

    def append_batch(self, events: Sequence[Mapping[str, Any]], *, flush: bool = False) -> dict[str, Any]:
        """Write events and their outbox rows in one SQLite transaction."""

        data = [self._normalize_event(event) for event in events]
        results: list[dict[str, Any]] = []
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                seen: dict[str, str] = {}
                for item in data:
                    event_id = item["event_id"]
                    payload = _canonical(item)
                    comparable = _canonical({key: value for key, value in item.items() if key != "created_at"})
                    previous = seen.get(event_id)
                    if previous is not None:
                        if previous != comparable:
                            results.append({"event_id": event_id, "status": "conflict", "event": item})
                        else:
                            results.append({"event_id": event_id, "status": "idempotent", "event": item})
                        continue
                    seen[event_id] = comparable
                    existing = connection.execute(
                        "SELECT payload_json FROM shadow_events WHERE event_id = ?", (event_id,)
                    ).fetchone()
                    if existing is not None:
                        comparable_existing = {key: value for key, value in json.loads(existing["payload_json"]).items() if key != "created_at"}
                        comparable_current = {key: value for key, value in item.items() if key != "created_at"}
                        if comparable_existing != comparable_current:
                            results.append({"event_id": event_id, "status": "conflict", "event": item})
                        else:
                            results.append({"event_id": event_id, "status": "idempotent", "event": item})
                        continue
                    connection.execute(
                        """INSERT INTO shadow_events(
                            event_id, event_type, root_task_id, revision_id, run_id,
                            created_at, schema_version, payload_json, lifecycle_status
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (event_id, item["event_type"], item["root_task_id"], item.get("revision_id"),
                         item.get("run_id"), item["created_at"], item["schema_version"], payload,
                         item.get("lifecycle_status", "TERMINAL")),
                    )
                    connection.execute(
                        """INSERT INTO jsonl_outbox(event_id, payload_json, created_at)
                           VALUES(?, ?, ?) ON CONFLICT(event_id) DO NOTHING""",
                        (event_id, payload, item["created_at"]),
                    )
                    results.append({"event_id": event_id, "status": "inserted", "event": item})
                connection.commit()
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise ShadowEventStoreBusy(f"shadow store busy: {self.database_path}") from exc
            raise
        flush_result = self.flush_pending(limit=len(data)) if flush and data else None
        return {"results": results, "flush": flush_result}

    def append(self, event: Mapping[str, Any], *, flush: bool = True) -> dict[str, Any]:
        result = self.append_batch([event], flush=flush)
        item = result["results"][0]
        if item["status"] == "conflict":
            raise EventConflictError(f"event_id reused with different payload: {item['event_id']}")
        response = {
            "event_id": item["event_id"],
            "inserted": item["status"] == "inserted",
            "idempotent": item["status"] == "idempotent",
            "event": item["event"],
        }
        if result["flush"] is not None:
            response["flush"] = result["flush"]
        return response

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM shadow_events WHERE event_id = ?", (str(event_id),)
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def list_events(self, root_task_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if root_task_id is None:
                rows = connection.execute("SELECT payload_json FROM shadow_events ORDER BY rowid").fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload_json FROM shadow_events WHERE root_task_id = ? ORDER BY rowid",
                    (str(root_task_id),),
                ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def read_event_log(self) -> tuple[list[dict[str, Any]], list[int]]:
        events: list[dict[str, Any]] = []
        corrupt_lines: list[int] = []
        if not self.event_log_path.exists():
            return events, corrupt_lines
        self._validate_managed_file(self.event_log_path, allow_missing=False)
        with self.event_log_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("event is not an object")
                    events.append(value)
                except (json.JSONDecodeError, ValueError):
                    corrupt_lines.append(line_number)
        return events, corrupt_lines

    def jsonl_paths(self) -> list[Path]:
        """Return current and closed segments without following symlinks."""

        paths = []
        if self.event_log_path.exists() and not self.event_log_path.is_symlink():
            paths.append(self.event_log_path)
        paths.extend(
            path for path in sorted(self.root.glob("shadow-events-*.jsonl"))
            if path != self.event_log_path and not path.is_symlink()
        )
        return paths

    def sqlite_storage_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.database_path) + suffix)
            if path.exists() and not path.is_symlink():
                total += path.stat().st_size
        return total

    def closed_jsonl_paths(self) -> list[Path]:
        return [path for path in self.jsonl_paths() if path != self.event_log_path]

    def rotate_jsonl(
        self,
        *,
        max_bytes: int,
        now: float | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Rotate the derived current segment under a process-safe file lock."""

        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if not self.event_log_path.exists():
            return {"rotated": False, "reason": "missing_current", "segment": None}
        # Lock a stable inode.  Locking the current segment itself would lose
        # mutual exclusion immediately after os.replace() creates its successor.
        lock_fd = self._open_private_lock(self.rotation_lock_path)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            self._validate_managed_file(self.event_log_path)
            if not self.event_log_path.exists():
                return {"rotated": False, "reason": "missing_current", "segment": None}
            flags = os.O_CREAT | os.O_RDWR | os.O_APPEND
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                current_fd = os.open(str(self.event_log_path), flags, 0o600)
            except OSError as exc:
                raise ShadowEventError("event log path is unsafe") from exc
            with os.fdopen(current_fd, "a+b") as current:
                try:
                    current.flush()
                    os.fsync(current.fileno())
                    size = current.seek(0, os.SEEK_END)
                    if not force and size < max_bytes:
                        return {"rotated": False, "reason": "below_limit", "segment": None, "bytes": size}
                    timestamp = datetime.fromtimestamp(
                        float(time.time() if now is None else now), tz=timezone.utc
                    ).strftime("%Y%m%d-%H%M%S")
                    sequence = 1
                    while True:
                        target = self.root / f"shadow-events-{timestamp}-{sequence:06d}.jsonl"
                        if not target.exists() and not target.is_symlink():
                            break
                        sequence += 1
                    os.replace(self.event_log_path, target)
                    os.chmod(target, 0o600)
                    create_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                    if hasattr(os, "O_NOFOLLOW"):
                        create_flags |= os.O_NOFOLLOW
                    fd = os.open(str(self.event_log_path), create_flags, 0o600)
                    os.close(fd)
                    os.chmod(self.event_log_path, 0o600)
                    return {"rotated": True, "reason": "limit" if not force else "forced", "segment": target.name, "bytes": size}
                finally:
                    fcntl.flock(current.fileno(), fcntl.LOCK_UN)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def retention_dry_run(self, *, cutoff: str, batch_size: int = 500) -> dict[str, Any]:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        protected = ("ACTIVE", "CLAIMED", "RECOVERY_CANDIDATE", "FLUSHING", "PENDING_OUTBOX", "FAILED_UNRESOLVED", "QUARANTINED")
        placeholders = ",".join("?" for _ in protected)
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT e.event_id, length(e.payload_json) AS bytes
                    FROM shadow_events e
                    LEFT JOIN jsonl_outbox o ON o.event_id = e.event_id
                    WHERE e.created_at < ?
                      AND upper(COALESCE(e.lifecycle_status, 'TERMINAL')) NOT IN ({placeholders})
                      AND (o.event_id IS NULL OR o.status = 'DELIVERED')
                    ORDER BY e.created_at LIMIT ?""",
                (cutoff, *protected, batch_size),
            ).fetchall()
        return {
            "cutoff": cutoff,
            "candidate_count": len(rows),
            "estimated_payload_bytes": sum(int(row["bytes"] or 0) for row in rows),
            "batch_size": batch_size,
        }

    def retention_execute(self, *, cutoff: str, batch_size: int = 500) -> dict[str, Any]:
        preview = self.retention_dry_run(cutoff=cutoff, batch_size=batch_size)
        if not preview["candidate_count"]:
            preview["deleted_count"] = 0
            preview["vacuum_pages_requested"] = 0
            return preview
        protected = ("ACTIVE", "CLAIMED", "RECOVERY_CANDIDATE", "FLUSHING", "PENDING_OUTBOX", "FAILED_UNRESOLVED", "QUARANTINED")
        placeholders = ",".join("?" for _ in protected)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    f"""SELECT e.event_id FROM shadow_events e
                        LEFT JOIN jsonl_outbox o ON o.event_id = e.event_id
                        WHERE e.created_at < ?
                          AND upper(COALESCE(e.lifecycle_status, 'TERMINAL')) NOT IN ({placeholders})
                          AND (o.event_id IS NULL OR o.status = 'DELIVERED')
                        ORDER BY e.created_at LIMIT ?""",
                    (cutoff, *protected, batch_size),
                ).fetchall()
                ids = [str(row["event_id"]) for row in rows]
                if ids:
                    marks = ",".join("?" for _ in ids)
                    connection.execute(f"DELETE FROM jsonl_outbox WHERE event_id IN ({marks}) AND status = 'DELIVERED'", ids)
                    connection.execute(f"DELETE FROM shadow_events WHERE event_id IN ({marks})", ids)
                connection.execute("PRAGMA incremental_vacuum(100)")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        preview["deleted_count"] = len(ids)
        preview["vacuum_pages_requested"] = 100
        return preview

    def recover_pending_outbox(self) -> int:
        with self._connection() as connection:
            cursor = connection.execute("UPDATE jsonl_outbox SET status = 'PENDING' WHERE status = 'FLUSHING'")
            connection.commit()
            return int(cursor.rowcount)

    def quarantine_corrupt_segments(self) -> list[str]:
        quarantine = self.root / "quarantine"
        found: list[str] = []
        for path in self.closed_jsonl_paths():
            if path.is_symlink():
                continue
            corrupt = False
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise ValueError("segment record is not an object")
                    except (json.JSONDecodeError, ValueError):
                        corrupt = True
                        break
            if corrupt:
                quarantine.mkdir(mode=0o700, exist_ok=True)
                os.chmod(quarantine, 0o700)
                target = quarantine / path.name
                os.replace(path, target)
                os.chmod(target, 0o600)
                found.append(path.name)
        return found

    def health_snapshot(self) -> dict[str, Any]:
        paths = self.jsonl_paths()
        sizes = {"sqlite_bytes": self.sqlite_storage_bytes(),
                 "jsonl_bytes": sum(path.stat().st_size for path in paths),
                 "segment_count": len(paths)}
        with self._connection() as connection:
            row = connection.execute("SELECT MIN(created_at), MAX(created_at) FROM shadow_events").fetchone()
            pending = connection.execute("SELECT COUNT(*) FROM jsonl_outbox WHERE status IN ('PENDING','FLUSHING')").fetchone()[0]
            failed = connection.execute("SELECT COUNT(*) FROM jsonl_outbox WHERE last_error IS NOT NULL").fetchone()[0]
        sizes.update({
            "oldest_event_at": row[0],
            "newest_event_at": row[1],
            "pending_outbox": int(pending),
            "failed_outbox": int(failed),
            "active_connections": self.active_connection_count,
        })
        return sizes

    def count(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM shadow_events").fetchone()
        return int(row["count"])
