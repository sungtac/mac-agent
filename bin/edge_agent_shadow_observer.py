"""Fail-open, privacy-minimal Telegram Shadow Observer.

The observer is intentionally provider-neutral.  It records ingress metadata
only and never invokes Telegram, a provider CLI, or a router.  SQLite remains
authoritative; JSONL is emitted by the event-store transactional outbox.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import logging
import math
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, Mapping

from edge_agent_shadow_event_store import EventConflictError, ShadowEventStore
from edge_agent_shadow_keyring import HMACKeyring, ShadowKeyError
from edge_agent_shadow_maintenance import (
    HARD_LIMIT,
    SOFT_LIMIT,
    ShadowMaintenance,
    ShadowMaintenanceConfig,
)
from edge_agent_task_identity import (
    revision_id,
    root_task_id,
    stable_hash,
    telegram_update_key,
)


LOG = logging.getLogger(__name__)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})
_DEFAULT_ROOT = "/Users/edge_ai/.edge-agent/phase1a-shadow"


@dataclass(frozen=True)
class ShadowObserverConfig:
    enabled: bool
    root: Path
    queue_size: int = 256
    busy_timeout_ms: int = 1000
    flush_timeout_seconds: float = 2.0
    db_batch_size: int = 50
    db_batch_max_wait_seconds: float = 0.010
    outbox_batch_size: int = 100
    body_hmac_key: bytes | None = None
    reason: str | None = None
    body_hmac_key_id: str | None = None
    sqlite_max_bytes: int = 512 * 1024 * 1024
    jsonl_segment_max_bytes: int = 256 * 1024 * 1024
    total_soft_limit_bytes: int = 768 * 1024 * 1024
    total_hard_limit_bytes: int = 1024 * 1024 * 1024
    retention_days: int = 30
    jsonl_retention_days: int = 14
    retention_batch_size: int = 500
    maintenance_interval_seconds: float = 3600.0
    jsonl_rotation_interval_seconds: float = 86400.0

    def __post_init__(self) -> None:
        for name in (
            "flush_timeout_seconds",
            "db_batch_max_wait_seconds",
            "maintenance_interval_seconds",
            "jsonl_rotation_interval_seconds",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")


def _bool_setting(value: str | None) -> tuple[bool, str | None]:
    if value is None or value.casefold() in _FALSE_VALUES:
        return False, None
    if value.casefold() in _TRUE_VALUES:
        return True, None
    return False, "invalid feature flag"


def _positive_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("must be positive")
    return parsed


def _positive_float(value: str | None, default: float) -> float:
    if value is None or value == "":
        return default
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("must be finite and positive")
    return parsed


def _forbidden_roots(env: Mapping[str, str]) -> list[Path]:
    home = Path.home()
    token_file = Path(env.get("TELEGRAM_AGENT_TOKEN_FILE", str(home / ".edge-agent" / "secrets" / "telegram" / "bot.token"))).expanduser()
    roots = [
        Path(env.get("EDGE_AGENT_STATE_DIR", str(home / ".edge-agent" / "state"))).expanduser(),
        Path(env.get("EDGE_AGENT_PLAN_DIR", str(home / ".edge-agent" / "plans"))).expanduser(),
        Path(env.get("EDGE_AGENT_SESSION_ROOT", str(home / ".edge-agent" / "sessions"))).expanduser(),
        Path(env.get("EDGE_AGENT_TELEGRAM_DELIVERY_ROOT", str(home / ".edge-agent" / "state" / "telegram-delivery"))).expanduser(),
        Path(env.get("TELEGRAM_CODEX_TASK_WORKTREE_ROOT", str(home / ".edge-agent-worktrees" / "telegram-tasks"))).expanduser(),
        Path(env.get("TELEGRAM_AGENT_SOURCE_REPO", str(home / "mac-agent"))).expanduser(),
        token_file.parent,
        home / ".edge-agent" / "secrets",
    ]
    return [path.resolve() for path in roots]


def load_config(environ: Mapping[str, str] | None = None) -> ShadowObserverConfig:
    env = os.environ if environ is None else environ
    enabled, reason = _bool_setting(env.get("EDGE_AGENT_SHADOW_OBSERVER_ENABLED"))
    root_value = env.get("EDGE_AGENT_SHADOW_ROOT", _DEFAULT_ROOT)
    if not root_value.strip():
        LOG.warning("invalid Shadow Observer root; disabled")
        return ShadowObserverConfig(False, Path(_DEFAULT_ROOT), reason="invalid root")
    root = Path(root_value).expanduser()
    if not enabled:
        if reason:
            LOG.warning("invalid Shadow Observer feature flag; disabled")
        return ShadowObserverConfig(False, root, reason=reason)
    try:
        queue_size = _positive_int(env.get("EDGE_AGENT_SHADOW_QUEUE_SIZE"), 256)
        busy_timeout_ms = _positive_int(env.get("EDGE_AGENT_SHADOW_DB_BUSY_TIMEOUT_MS"), 1000)
        flush_timeout = _positive_float(env.get("EDGE_AGENT_SHADOW_FLUSH_TIMEOUT_MS"), 2000.0) / 1000.0
        db_batch_size = _positive_int(env.get("EDGE_AGENT_SHADOW_DB_BATCH_SIZE"), 50)
        db_batch_wait = _positive_float(env.get("EDGE_AGENT_SHADOW_DB_BATCH_MAX_WAIT_MS"), 10.0) / 1000.0
        outbox_batch_size = _positive_int(env.get("EDGE_AGENT_SHADOW_OUTBOX_BATCH_SIZE"), 100)
        sqlite_max_bytes = _positive_int(env.get("EDGE_AGENT_SHADOW_SQLITE_MAX_BYTES"), 512 * 1024 * 1024)
        jsonl_segment_max_bytes = _positive_int(env.get("EDGE_AGENT_SHADOW_JSONL_SEGMENT_MAX_BYTES"), 256 * 1024 * 1024)
        total_soft_limit_bytes = _positive_int(env.get("EDGE_AGENT_SHADOW_TOTAL_SOFT_LIMIT_BYTES"), 768 * 1024 * 1024)
        total_hard_limit_bytes = _positive_int(env.get("EDGE_AGENT_SHADOW_TOTAL_HARD_LIMIT_BYTES"), 1024 * 1024 * 1024)
        retention_days = _positive_int(env.get("EDGE_AGENT_SHADOW_RETENTION_DAYS"), 30)
        jsonl_retention_days = _positive_int(env.get("EDGE_AGENT_SHADOW_JSONL_RETENTION_DAYS"), 14)
        retention_batch_size = _positive_int(env.get("EDGE_AGENT_SHADOW_RETENTION_BATCH_SIZE"), 500)
        maintenance_interval = _positive_float(env.get("EDGE_AGENT_SHADOW_MAINTENANCE_INTERVAL_SECONDS"), 3600.0)
        jsonl_rotation_interval = _positive_float(env.get("EDGE_AGENT_SHADOW_JSONL_ROTATION_INTERVAL_SECONDS"), 86400.0)
        if not total_soft_limit_bytes < total_hard_limit_bytes:
            raise ValueError("soft limit must be below hard limit")
    except (TypeError, ValueError) as exc:
        LOG.warning("invalid Shadow Observer setting; disabled: %s", exc)
        return ShadowObserverConfig(False, root, reason="invalid configuration")

    state_root = Path(env.get("EDGE_AGENT_STATE_DIR", str(Path.home() / ".edge-agent" / "state"))).expanduser()
    try:
        canonical_root = root.resolve()
        if canonical_root == state_root.resolve():
            LOG.warning("Shadow Observer root equals operational state root; disabled")
            return ShadowObserverConfig(False, root, reason="operational state root")
        if any(canonical_root == forbidden or forbidden in canonical_root.parents for forbidden in _forbidden_roots(env)):
            LOG.warning("Shadow Observer root overlaps a protected path; disabled")
            return ShadowObserverConfig(False, root, reason="protected root")
    except OSError:
        return ShadowObserverConfig(False, root, reason="root comparison failed")

    key: bytes | None = None
    key_id: str | None = None
    key_path = env.get("EDGE_AGENT_SHADOW_BODY_HMAC_KEY_FILE", "").strip()
    if key_path:
        candidate_key_path = Path(key_path).expanduser()
        try:
            keyring = HMACKeyring(candidate_key_path)
            key = keyring.key
            key_id = keyring.key_id
        except ShadowKeyError as exc:
            if candidate_key_path.exists() or candidate_key_path.is_symlink():
                LOG.warning("Shadow Observer HMAC key rejected; disabled: %s", type(exc).__name__)
                return ShadowObserverConfig(False, root, reason="invalid HMAC key")
            LOG.warning("Shadow Observer HMAC key unavailable; body fingerprint is UNKNOWN")
        except (OSError, ValueError):
            LOG.warning("Shadow Observer HMAC key unavailable; body fingerprint is UNKNOWN")
    return ShadowObserverConfig(
        True,
        root,
        queue_size=queue_size,
        busy_timeout_ms=busy_timeout_ms,
        flush_timeout_seconds=flush_timeout,
        db_batch_size=db_batch_size,
        db_batch_max_wait_seconds=db_batch_wait,
        outbox_batch_size=outbox_batch_size,
        body_hmac_key=key,
        body_hmac_key_id=key_id,
        sqlite_max_bytes=sqlite_max_bytes,
        jsonl_segment_max_bytes=jsonl_segment_max_bytes,
        total_soft_limit_bytes=total_soft_limit_bytes,
        total_hard_limit_bytes=total_hard_limit_bytes,
        retention_days=retention_days,
        jsonl_retention_days=jsonl_retention_days,
        retention_batch_size=retention_batch_size,
        maintenance_interval_seconds=maintenance_interval,
        jsonl_rotation_interval_seconds=jsonl_rotation_interval,
    )


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _message_from_update(update: Any) -> tuple[Any | None, str]:
    message = _get(update, "effective_message")
    if message is not None:
        return message, "edited_message" if _get(update, "edited_message") is not None else "message"
    for name in ("message", "edited_message"):
        message = _get(update, name)
        if message is not None:
            return message, name
    return None, "unsupported_update_type"


def _chat_scope(chat: Any) -> str:
    chat_type = str(_get(chat, "type", "" )).casefold()
    if chat_type in {"group", "supergroup", "channel", "shared_channel"}:
        return "group" if chat_type in {"group", "supergroup"} else chat_type
    return "private"


def _attachment_metadata(message: Any) -> list[dict[str, Any]]:
    candidates = (
        ("document", "document"), ("photo", "photo"), ("audio", "audio"),
        ("voice", "voice"), ("video", "video"), ("video_note", "video_note"),
        ("animation", "animation"), ("sticker", "sticker"),
    )
    result: list[dict[str, Any]] = []
    for attr, mime_default in candidates:
        value = _get(message, attr)
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            unique_id = _get(item, "file_unique_id")
            result.append({
                "file_unique_id_hash": stable_hash(str(unique_id)) if unique_id is not None else None,
                "content_hash": None,
                "size": _get(item, "file_size"),
                "mime_type": _get(item, "mime_type", mime_default),
                "hash_status": "UNKNOWN",
            })
    return result


def extract_ingress_metadata(
    update: Any,
    *,
    bot_id: str | int,
    bot_role: str,
    legacy_target: str | None = None,
    received_at: str | None = None,
    hmac_key: bytes | None = None,
    hmac_key_id: str | None = None,
) -> dict[str, Any] | None:
    message, update_type = _message_from_update(update)
    if message is None:
        return None
    chat = _get(message, "chat")
    chat_id = _get(chat, "id")
    message_id = _get(message, "message_id")
    update_id = _get(update, "update_id")
    if chat_id is None or message_id is None or update_id is None:
        return None
    scope = _chat_scope(chat)
    shared_chat_id = chat_id if scope in {"group", "supergroup", "channel", "shared_channel"} else None
    identity = {
        "platform": "telegram",
        "chat_scope": scope,
        "message_id": int(message_id),
        "shared_chat_id": shared_chat_id,
        "bot_id": bot_id,
        "chat_id": chat_id,
    }
    root = root_task_id(**identity)
    text = _get(message, "text") or _get(message, "caption")
    body_hash = None
    body_status = "UNKNOWN"
    if hmac_key is not None and text is not None:
        body_hash = hmac.new(hmac_key, str(text).encode("utf-8"), hashlib.sha256).hexdigest()
        body_status = "HMAC-SHA256"
    attachments = _attachment_metadata(message)
    edit_date = _get(message, "edit_date")
    edit_version = int(edit_date.timestamp()) if hasattr(edit_date, "timestamp") else int(edit_date or 0)
    # Body HMAC is an observability fingerprint only.  Excluding it from the
    # ingress revision keeps logical event identity stable across key rotation.
    revision = revision_id(root, message_edit_version=max(0, edit_version), body_hash=None, attachments=attachments)
    cross_identity = (
        {"platform": "telegram", "chat_scope": scope, "shared_chat_id": shared_chat_id, "message_id": int(message_id)}
        if scope in {"group", "supergroup", "channel", "shared_channel"}
        else {"platform": "telegram", "chat_scope": scope, "bot_id": bot_id, "chat_id": chat_id, "message_id": int(message_id)}
    )
    return {
        "event_type": "ingress_observed",
        "platform": "telegram",
        "bot_id": str(bot_id),
        "bot_role": str(bot_role),
        "chat_scope": scope,
        "chat_id": str(chat_id),
        "shared_chat_id": str(shared_chat_id) if shared_chat_id is not None else None,
        "message_id": int(message_id),
        "thread_id": _get(message, "message_thread_id"),
        "update_id": int(update_id),
        "update_type": update_type,
        "edit_date": edit_date.isoformat() if hasattr(edit_date, "isoformat") else edit_date,
        "message_length": len(str(text)) if text is not None else 0,
        "attachment_count": len(attachments),
        "attachment_metadata": attachments,
        "legacy_target": legacy_target,
        "received_at": received_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root_task_id": root,
        "cross_bot_message_key": stable_hash(cross_identity),
        "telegram_update_key": telegram_update_key("telegram", bot_id=bot_id, chat_id=chat_id, update_id=int(update_id)),
        "revision_id": revision,
        "body_hash": body_hash,
        "body_hash_status": body_status,
        "body_hmac_key_id": hmac_key_id if body_hash is not None else None,
        "task_type": "UNKNOWN",
        "risk_level": "UNKNOWN",
        "route_policy_version": None,
        "observed_provider_role": legacy_target,
    }


def build_shadow_events(metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    root = str(metadata["root_task_id"])
    revision = str(metadata["revision_id"])
    logical_base = {
        "event_type": "ingress_observed",
        "root_task_id": root,
        "revision_id": revision,
        "message_identity_hash": metadata.get("cross_bot_message_key"),
        "body_hash": metadata.get("body_hash"),
        "body_hash_status": metadata.get("body_hash_status", "UNKNOWN"),
        "body_hmac_key_id": metadata.get("body_hmac_key_id"),
        "attachment_hashes": [item.get("file_unique_id_hash") for item in metadata.get("attachment_metadata", [])],
        "classification": {"task_type": metadata.get("task_type", "UNKNOWN"), "risk_level": metadata.get("risk_level", "UNKNOWN")},
    }
    logical_base["event_id"] = stable_hash({"event_type": logical_base["event_type"], "root_task_id": root, "revision_id": revision})
    observation = {
        "event_type": "bot_observation",
        "root_task_id": root,
        "revision_id": revision,
        "event_id": stable_hash({"event_type": "bot_observation", "telegram_update_key": metadata.get("telegram_update_key")} ),
        "bot_id": metadata.get("bot_id"),
        "bot_role": metadata.get("bot_role"),
        "telegram_update_key": metadata.get("telegram_update_key"),
        "update_type": metadata.get("update_type"),
        "message_length": metadata.get("message_length", 0),
        "attachment_count": metadata.get("attachment_count", 0),
        "observed_provider_role": metadata.get("observed_provider_role"),
    }
    return [logical_base, observation]


NEW = "NEW"
RUNNING = "RUNNING"
STOPPING = "STOPPING"
DRAINING = "DRAINING"
STOPPED = "STOPPED"
FAILED = "FAILED"


class ShadowObserver:
    def __init__(self, config: ShadowObserverConfig, *, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.log = logger or LOG
        self.store: ShadowEventStore | None = None
        self.maintenance: ShadowMaintenance | None = None
        self._queue: queue.Queue[dict[str, Any]] | None = None
        self._stop = threading.Event()
        self._force_stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._state = NEW
        self._stats = {
            "submitted": 0,
            "accepted": 0,
            "processed": 0,
            "dropped_queue_full": 0,
            "rejected_stopping": 0,
            "abandoned_shutdown_timeout": 0,
            "failed_store": 0,
            "dropped_disk_budget": 0,
            "errors": 0,
            "jsonl_errors": 0,
            "queue_high_watermark": 0,
            "queue_remaining_at_stop": 0,
            "worker_alive_after_stop": 0,
            "active_key_id": self.config.body_hmac_key_id,
        }
        self._last_error_at = 0.0
        self._last_maintenance_at = 0.0

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def active(self) -> bool:
        return self.state == RUNNING and self._thread is not None and self._thread.is_alive()

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            result = dict(self._stats)
            # Preserve the A2 names while exposing the explicit A3 accounting.
            result["enqueued"] = result["accepted"]
            result["dropped"] = result["dropped_queue_full"]
            result["worker_alive_after_stop"] = int(self._thread is not None and self._thread.is_alive() and self._state == STOPPED)
            result["state"] = self._state
            return result

    def _set_state(self, state: str) -> None:
        with self._lock:
            self._state = state

    def start(self) -> bool:
        with self._lifecycle_lock:
            if not self.config.enabled:
                return False
            if self.active:
                return True
            if self.state not in {NEW, STOPPED}:
                return False
            try:
                self.store = ShadowEventStore(self.config.root, busy_timeout_ms=self.config.busy_timeout_ms)
                self.maintenance = ShadowMaintenance(
                    self.store,
                    config=ShadowMaintenanceConfig(
                        retention_days=self.config.retention_days,
                        jsonl_retention_days=self.config.jsonl_retention_days,
                        sqlite_max_bytes=self.config.sqlite_max_bytes,
                        jsonl_segment_max_bytes=self.config.jsonl_segment_max_bytes,
                        total_soft_limit_bytes=self.config.total_soft_limit_bytes,
                        total_hard_limit_bytes=self.config.total_hard_limit_bytes,
                        retention_batch_size=self.config.retention_batch_size,
                        maintenance_interval_seconds=self.config.maintenance_interval_seconds,
                        jsonl_rotation_interval_seconds=self.config.jsonl_rotation_interval_seconds,
                    ),
                )
                self._queue = queue.Queue(maxsize=self.config.queue_size)
                self._stop.clear()
                self._force_stop.clear()
                self._set_state(RUNNING)
                try:
                    self.maintenance.write_health_snapshot(observer_stats=self.stats)
                except Exception as exc:
                    self._record_error(exc)
                self._thread = threading.Thread(target=self._run, name="edge-agent-shadow-observer", daemon=True)
                self._thread.start()
                return True
            except Exception as exc:
                self._record_error(exc)
                self.store = None
                self.maintenance = None
                self._queue = None
                self._thread = None
                self._set_state(FAILED)
                return False

    def record_update(self, update: Any, *, bot_id: str | int, bot_role: str, legacy_target: str | None = None) -> bool:
        if not self.active or self._queue is None:
            return False
        try:
            metadata = extract_ingress_metadata(
                update,
                bot_id=bot_id,
                bot_role=bot_role,
                legacy_target=legacy_target,
                hmac_key=self.config.body_hmac_key,
                hmac_key_id=self.config.body_hmac_key_id,
            )
            return self.enqueue(metadata) if metadata is not None else False
        except Exception as exc:
            self._record_error(exc)
            return False

    def enqueue(self, metadata: Mapping[str, Any] | None) -> bool:
        if metadata is None:
            return False
        with self._lock:
            self._stats["submitted"] += 1
            if self._state != RUNNING or self._queue is None:
                self._stats["rejected_stopping"] += 1
                return False
            target_queue = self._queue
        try:
            target_queue.put_nowait(dict(metadata))
        except queue.Full:
            with self._lock:
                self._stats["dropped_queue_full"] += 1
            return False
        with self._lock:
            self._stats["accepted"] += 1
            self._stats["queue_high_watermark"] = max(self._stats["queue_high_watermark"], target_queue.qsize())
        return True

    def _record_error(self, exc: BaseException) -> None:
        with self._lock:
            self._stats["errors"] += 1
        now = time.monotonic()
        if now - self._last_error_at >= 1.0:
            self._last_error_at = now
            self.log.warning("Shadow Observer fail-open error: %s", exc)

    def _take_batch(self) -> list[dict[str, Any]]:
        assert self._queue is not None
        try:
            first = self._queue.get(timeout=0.05)
        except queue.Empty:
            return []
        batch = [first]
        deadline = time.monotonic() + self.config.db_batch_max_wait_seconds
        while len(batch) < self.config.db_batch_size:
            if self._force_stop.is_set():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(self._queue.get(timeout=min(remaining, 0.005)))
            except queue.Empty:
                break
        return batch

    def _abandon_queued(self) -> int:
        if self._queue is None:
            return 0
        abandoned = 0
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            else:
                self._queue.task_done()
                abandoned += 1
        if abandoned:
            with self._lock:
                self._stats["abandoned_shutdown_timeout"] += abandoned
        return abandoned

    def _maintenance_tick(self) -> str | None:
        if self.maintenance is None:
            return None
        now = time.monotonic()
        if now - self._last_maintenance_at < self.config.maintenance_interval_seconds:
            try:
                return self.maintenance.disk_state()
            except Exception as exc:
                self._record_error(exc)
                return None
        self._last_maintenance_at = now
        try:
            disk_state = self.maintenance.disk_state()
            if disk_state == SOFT_LIMIT:
                self.maintenance.enforce_soft_limit()
                disk_state = self.maintenance.disk_state()
            self.maintenance.rotate_if_needed()
            self.maintenance.write_health_snapshot(observer_stats=self.stats)
            return disk_state
        except Exception as exc:
            self._record_error(exc)
            return None

    def _run(self) -> None:
        assert self._queue is not None
        try:
            while True:
                if self._stop.is_set() and self._queue.empty():
                    break
                batch = self._take_batch()
                if not batch:
                    self._maintenance_tick()
                    continue
                if self._force_stop.is_set():
                    with self._lock:
                        self._stats["abandoned_shutdown_timeout"] += len(batch)
                    for _ in batch:
                        self._queue.task_done()
                    continue
                try:
                    if self.store is None:
                        raise RuntimeError("Shadow Event Store unavailable")
                    if self.maintenance is not None:
                        disk_state = self._maintenance_tick()
                        if disk_state == HARD_LIMIT:
                            with self._lock:
                                self._stats["dropped_disk_budget"] += len(batch)
                            continue
                    events: list[dict[str, Any]] = []
                    event_to_item: dict[str, int] = {}
                    for index, metadata in enumerate(batch):
                        for event in build_shadow_events(metadata):
                            events.append(event)
                            event_to_item[str(event["event_id"])] = index
                    result = self.store.append_batch(events, flush=False)
                    failed_items = {event_to_item[item["event_id"]] for item in result["results"] if item["status"] == "conflict"}
                    with self._lock:
                        self._stats["processed"] += len(batch) - len(failed_items)
                        self._stats["failed_store"] += len(failed_items)
                    if failed_items:
                        self._record_error(EventConflictError("batch event conflict"))
                except Exception as exc:
                    with self._lock:
                        self._stats["failed_store"] += len(batch)
                    self._record_error(exc)
                else:
                    try:
                        flush_result = self.store.flush_pending(
                            limit=self.config.outbox_batch_size,
                            timeout_seconds=self.config.flush_timeout_seconds,
                        )
                        if flush_result.get("errors"):
                            with self._lock:
                                self._stats["jsonl_errors"] += int(flush_result["errors"])
                        if self.maintenance is not None:
                            self._maintenance_tick()
                    except Exception as exc:
                        # SQLite is authoritative; JSONL failure leaves the
                        # committed outbox available for recovery.
                        self._record_error(exc)
                        with self._lock:
                            self._stats["jsonl_errors"] += 1
                finally:
                    for _ in batch:
                        self._queue.task_done()
        except BaseException as exc:
            self._record_error(exc)
            self._set_state(FAILED)

    def stop(self, timeout: float | None = None) -> dict[str, int]:
        with self._lifecycle_lock:
            if self.state in {STOPPED, NEW, FAILED} and self._thread is None:
                if self.state == NEW:
                    self._set_state(STOPPED)
                return self.stats
            if self._thread is None:
                self._set_state(STOPPED)
                return self.stats
            self._set_state(STOPPING)
            self._stop.set()
            thread = self._thread
            wait = self.config.flush_timeout_seconds if timeout is None else max(0.0, timeout)
            deadline = time.monotonic() + wait
            self._set_state(DRAINING)
            while thread.is_alive() and time.monotonic() < deadline:
                thread.join(min(0.05, max(0.0, deadline - time.monotonic())))
            if thread.is_alive() or (self._queue is not None and not self._queue.empty()):
                self._force_stop.set()
                self._abandon_queued()
            # A worker cannot be force-killed safely in Python.  It is limited
            # to bounded SQLite/JSONL operations, so final join guarantees the
            # lifecycle contract before this method returns.
            thread.join()
            if self.store is not None:
                try:
                    flush_result = self.store.flush_pending(
                        limit=self.config.outbox_batch_size,
                        timeout_seconds=wait,
                    )
                    if flush_result.get("errors"):
                        with self._lock:
                            self._stats["jsonl_errors"] += int(flush_result["errors"])
                except Exception as exc:
                    self._record_error(exc)
            with self._lock:
                self._stats["queue_remaining_at_stop"] = self._queue.qsize() if self._queue is not None else 0
                self._stats["worker_alive_after_stop"] = int(thread.is_alive())
            self._thread = None
            self._set_state(STOPPED)
            return self.stats

    def health_snapshot(self) -> dict[str, Any]:
        """Return non-sensitive health data without blocking legacy callers."""

        if self.maintenance is None:
            return {"enabled": False, "lifecycle": self.state, **self.stats}
        try:
            return self.maintenance.health_snapshot(observer_stats=self.stats)
        except Exception as exc:
            self._record_error(exc)
            return {"enabled": True, "lifecycle": self.state, "disk_state": "READ_ONLY_DEGRADED", **self.stats}
