#!/usr/bin/env python3
"""Durable, bounded inter-agent message bus and task graph.

This is intentionally dependency-free.  Telegram adapters and terminal
workers use the same append-only-ish JSON journal, so a process restart does
not turn a peer message or a child task into an invisible side effect.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterable, Mapping

from edge_agent_agent_message import (
    AGENT_ROLES,
    AgentMessage,
    AgentMessageError,
    build_message,
    deduplication_key,
    load_signing_key,
    load_verification_keys,
    verify_message,
)
from edge_agent_secure_paths import ensure_private_directory, open_lock


SCHEMA = "edge_agent.message_bus.v1"
TASK_SCHEMA = "edge_agent.task_graph.v1"
MAX_MESSAGES_PER_SESSION = 2000
MAX_TASKS_PER_SESSION = 256
DEFAULT_LEASE_SECONDS = 90.0


def _safe(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text or any(char in text for char in "/\\\x00") or len(text) > 160:
        raise ValueError(f"unsafe {name}")
    return text


def _now() -> float:
    return time.time()


def _message_from_dict(value: Mapping[str, Any]) -> AgentMessage:
    payload = dict(value)
    payload["to"] = tuple(payload.get("to") or ())
    payload["evidence_refs"] = tuple(payload.get("evidence_refs") or ())
    return AgentMessage(**payload)


def message_from_dict(value: Mapping[str, Any]) -> AgentMessage:
    """Rehydrate a validated bus message for a dispatcher or observer."""
    return _message_from_dict(value)


def _message_id(message: AgentMessage) -> str:
    return hashlib.sha256(
        message.canonical_bytes() + b"|" + message.signature.encode("ascii", "ignore")
    ).hexdigest()


class MessageBusError(ValueError):
    """Raised when a bus operation cannot preserve trust or lifecycle state."""


class MessageBus:
    """Cross-process durable inboxes with leases, acknowledgements and a DAG."""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        configured = root or os.environ.get(
            "EDGE_AGENT_MESSAGE_BUS_ROOT",
            str(Path.home() / ".edge-agent" / "state" / "message-bus"),
        )
        self.root = Path(configured).expanduser()
        ensure_private_directory(self.root)

    def _path(self, session_id: str) -> Path:
        return self.root / f"{_safe(session_id, 'session_id')}.json"

    def _lock(self):
        return open_lock(self.root / ".bus.lock")

    def _read(self, session_id: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(self._path(session_id).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write(self, session_id: str, payload: Mapping[str, Any]) -> None:
        target = self._path(session_id)
        descriptor, temporary = tempfile.mkstemp(prefix=".bus-", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(dict(payload), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _empty(session_id: str, request: str = "", max_rounds: int = 3) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "session_id": session_id,
            "request": str(request)[:4000],
            "status": "active",
            "max_rounds": max(1, min(8, int(max_rounds))),
            "messages": [],
            "tasks": {},
            "checkpoints": {},
            "events": [],
            "created_epoch": _now(),
            "updated_epoch": _now(),
        }

    def create_session(self, session_id: str, request: str = "", *, max_rounds: int = 3) -> dict[str, Any]:
        session_id = _safe(session_id, "session_id")
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            current = self._read(session_id)
            if current is not None:
                return current
            payload = self._empty(session_id, request, max_rounds)
            self._write(session_id, payload)
            return payload
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load_or_create(self, session_id: str, request: str = "") -> dict[str, Any]:
        return self._read(session_id) or self._empty(session_id, request)

    @staticmethod
    def _event(payload: dict[str, Any], kind: str, **fields: Any) -> None:
        events = list(payload.get("events") or [])
        events.append({"kind": kind, "epoch": _now(), **fields})
        payload["events"] = events[-4000:]
        payload["updated_epoch"] = _now()

    @staticmethod
    def _verification_key(key: object | None) -> object:
        if key is not None:
            return key
        keyring = os.environ.get("EDGE_AGENT_MESSAGE_KEYRING_DIR", "").strip()
        if keyring:
            return load_verification_keys(keyring)
        path = os.environ.get("EDGE_AGENT_MESSAGE_KEY_FILE", "").strip()
        if path:
            return load_signing_key(path)
        raise MessageBusError("message verification key is not configured")

    def publish(
        self,
        message: AgentMessage,
        *,
        verification_key: object | None = None,
        request: str = "",
    ) -> dict[str, Any]:
        """Verify and enqueue a message exactly once for all recipients."""
        try:
            verify_message(message, self._verification_key(verification_key))
        except (AgentMessageError, OSError, ValueError) as exc:
            raise MessageBusError(f"message rejected: {exc}") from exc
        session_id = _safe(message.session_id, "session_id")
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = self._load_or_create(session_id, request)
            if payload.get("schema") != SCHEMA:
                raise MessageBusError("unsupported message bus schema")
            if message.round > int(payload.get("max_rounds", 3)):
                raise MessageBusError("message round exceeds session budget")
            messages = list(payload.get("messages") or [])
            message_id = _message_id(message)
            for item in messages:
                if item.get("message_id") == message_id or item.get("deduplication_key") == deduplication_key(message):
                    return dict(item)
            if len(messages) >= MAX_MESSAGES_PER_SESSION:
                raise MessageBusError("session message budget exhausted")
            item = {
                "message_id": message_id,
                "deduplication_key": deduplication_key(message),
                "message": message.to_dict(),
                "status": "queued",
                "lease_owner": "",
                "lease_until": 0.0,
                "deliveries": {
                    role: {"status": "queued", "lease_owner": "", "lease_until": 0.0}
                    for role in message.to
                },
                "created_epoch": _now(),
            }
            messages.append(item)
            payload["messages"] = messages
            tasks = dict(payload.get("tasks") or {})
            tasks.setdefault(
                message.task_id,
                {
                    "schema": TASK_SCHEMA,
                    "task_id": message.task_id,
                    "parent_task_id": "",
                    "owner": message.from_role,
                    "purpose": message.purpose,
                    "status": "waiting",
                    "depends_on": [],
                    "created_epoch": _now(),
                    "updated_epoch": _now(),
                },
            )
            payload["tasks"] = tasks
            self._event(payload, "message_published", message_id=message_id, from_role=message.from_role, to=list(message.to), task_id=message.task_id)
            self._write(session_id, payload)
            return item
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def claim(self, role: str, *, session_id: str | None = None, owner: str | None = None, limit: int = 20, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> list[dict[str, Any]]:
        role = _safe(role, "role")
        if role not in AGENT_ROLES:
            raise MessageBusError("unknown agent role")
        owner = _safe(owner or role, "lease_owner")
        limit = max(1, min(100, int(limit)))
        now = _now()
        paths = [self._path(session_id)] if session_id else sorted(self.root.glob("*.json"))
        claimed: list[dict[str, Any]] = []
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            for path in paths:
                if len(claimed) >= limit:
                    break
                try:
                    current = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
                    continue
                if not isinstance(current, dict) or current.get("schema") != SCHEMA:
                    continue
                changed = False
                for item in current.get("messages") or []:
                    message = _message_from_dict(item.get("message") or {})
                    targets = set(message.to)
                    delivery = (item.setdefault("deliveries", {}).setdefault(
                        role, {"status": item.get("status", "queued"), "lease_owner": item.get("lease_owner", ""), "lease_until": item.get("lease_until", 0.0)}
                    ) if role in targets else None)
                    if delivery is None or delivery.get("status") in {"acked", "failed"}:
                        continue
                    if delivery.get("status") == "leased" and float(delivery.get("lease_until", 0.0)) > now and delivery.get("lease_owner") != owner:
                        continue
                    delivery["status"] = "leased"
                    delivery["lease_owner"] = owner
                    delivery["lease_until"] = now + max(1.0, min(900.0, float(lease_seconds)))
                    claimed.append(dict(item))
                    changed = True
                    if len(claimed) >= limit:
                        break
                if changed:
                    self._event(current, "messages_claimed", role=role, owner=owner, count=len(claimed))
                    self._write(str(current["session_id"]), current)
            return claimed
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def acknowledge(self, session_id: str, message_id: str, *, owner: str) -> bool:
        session_id = _safe(session_id, "session_id")
        owner = _safe(owner, "lease_owner")
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = self._read(session_id)
            if payload is None:
                return False
            for item in payload.get("messages") or []:
                if item.get("message_id") != message_id:
                    continue
                deliveries = item.get("deliveries") or {}
                delivery = deliveries.get(owner) or next(
                    (value for value in deliveries.values() if value.get("lease_owner") == owner),
                    None,
                )
                if delivery is None:
                    raise MessageBusError("message recipient lease is missing")
                if delivery.get("lease_owner") != owner and delivery.get("status") != "acked":
                    raise MessageBusError("message lease owner mismatch")
                delivery["status"] = "acked"
                delivery["acked_epoch"] = _now()
                if deliveries and all(value.get("status") == "acked" for value in deliveries.values()):
                    item["status"] = "acked"
                    item["acked_epoch"] = _now()
                self._event(payload, "message_acknowledged", message_id=message_id, owner=owner)
                self._write(session_id, payload)
                return True
            return False
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def release(
        self,
        session_id: str,
        message_id: str,
        *,
        owner: str,
        error: str = "",
        requeue: bool = True,
    ) -> str:
        """Release a lease after a handler failure without losing the message."""
        session_id = _safe(session_id, "session_id")
        owner = _safe(owner, "lease_owner")
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = self._read(session_id)
            if payload is None:
                return ""
            for item in payload.get("messages") or []:
                if item.get("message_id") != message_id:
                    continue
                deliveries = item.get("deliveries") or {}
                delivery = next(
                    (value for value in deliveries.values() if value.get("lease_owner") == owner),
                    None,
                )
                if delivery is None:
                    raise MessageBusError("message lease owner mismatch")
                attempts = int(delivery.get("attempts", 0)) + 1
                delivery["attempts"] = attempts
                delivery["last_error"] = str(error)[:500]
                delivery["status"] = "queued" if requeue and attempts < 3 else "failed"
                delivery["lease_owner"] = ""
                delivery["lease_until"] = 0.0
                self._event(payload, "message_released", message_id=message_id, owner=owner, requeue=delivery["status"] == "queued")
                self._write(session_id, payload)
                return str(delivery["status"])
            return ""
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def checkpoint(self, session_id: str, task_id: str, phase: str, status: str, *, summary: str = "") -> dict[str, Any]:
        """Persist the last safe boundary so a restarted worker can resume."""
        session_id = _safe(session_id, "session_id")
        task_id = _safe(task_id, "task_id")
        phase = _safe(phase, "phase")
        if status not in {"claimed", "running", "completed", "failed", "cancelled"}:
            raise MessageBusError("invalid checkpoint status")
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = self._read(session_id)
            if payload is None:
                raise MessageBusError("checkpoint session does not exist")
            checkpoints = dict(payload.get("checkpoints") or {})
            key = f"{task_id}:{phase}"
            value = {
                "task_id": task_id,
                "phase": phase,
                "status": status,
                "summary": str(summary)[:800],
                "updated_epoch": _now(),
            }
            checkpoints[key] = value
            payload["checkpoints"] = checkpoints
            self._event(payload, "checkpoint_written", task_id=task_id, phase=phase, status=status)
            self._write(session_id, payload)
            return dict(value)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def recoverable(self, session_id: str) -> list[dict[str, Any]]:
        """Return non-terminal task checkpoints suitable for a restarted worker."""
        payload = self._read(_safe(session_id, "session_id")) or {}
        checkpoints = payload.get("checkpoints") or {}
        result = []
        for value in checkpoints.values():
            if value.get("status") not in {"completed", "cancelled"}:
                result.append(dict(value))
        return sorted(result, key=lambda item: (str(item.get("task_id")), str(item.get("phase"))))

    def spawn_task(self, session_id: str, task_id: str, *, owner: str, purpose: str, parent_task_id: str = "", depends_on: Iterable[str] = ()) -> dict[str, Any]:
        session_id = _safe(session_id, "session_id")
        task_id = _safe(task_id, "task_id")
        owner = _safe(owner, "owner")
        if owner not in AGENT_ROLES:
            raise MessageBusError("unknown task owner")
        dependencies = tuple(dict.fromkeys(_safe(item, "dependency") for item in depends_on if str(item).strip()))
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = self._load_or_create(session_id)
            tasks = dict(payload.get("tasks") or {})
            if task_id in tasks:
                return dict(tasks[task_id])
            if len(tasks) >= MAX_TASKS_PER_SESSION:
                raise MessageBusError("session task budget exhausted")
            if parent_task_id:
                _safe(parent_task_id, "parent_task_id")
                if parent_task_id not in tasks:
                    raise MessageBusError("parent task does not exist")
            if any(item not in tasks for item in dependencies):
                raise MessageBusError("task dependency does not exist")
            task = {
                "schema": TASK_SCHEMA,
                "task_id": task_id,
                "parent_task_id": parent_task_id,
                "owner": owner,
                "purpose": _safe(purpose, "purpose"),
                "status": "ready" if not dependencies else "blocked",
                "depends_on": list(dependencies),
                "created_epoch": _now(),
                "updated_epoch": _now(),
            }
            tasks[task_id] = task
            payload["tasks"] = tasks
            self._event(payload, "task_spawned", task_id=task_id, parent_task_id=parent_task_id, owner=owner)
            self._write(session_id, payload)
            return dict(task)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def update_task(self, session_id: str, task_id: str, status: str, *, summary: str = "") -> dict[str, Any]:
        allowed = {"blocked", "ready", "running", "completed", "failed", "cancelled"}
        if status not in allowed:
            raise MessageBusError("invalid task status")
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = self._read(_safe(session_id, "session_id"))
            if payload is None or task_id not in (payload.get("tasks") or {}):
                raise MessageBusError("task does not exist")
            task = payload["tasks"][task_id]
            task["status"] = status
            task["summary"] = str(summary)[:1600]
            task["updated_epoch"] = _now()
            if status in {"completed", "failed", "cancelled"}:
                task["completed_epoch"] = _now()
            self._event(payload, "task_updated", task_id=task_id, status=status)
            self._write(str(payload["session_id"]), payload)
            return dict(task)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def ready_tasks(self, session_id: str, *, owner: str | None = None) -> list[dict[str, Any]]:
        payload = self._read(_safe(session_id, "session_id")) or {}
        tasks = payload.get("tasks") or {}
        result = []
        for task in tasks.values():
            if task.get("status") not in {"ready", "blocked"}:
                continue
            if owner and task.get("owner") != owner:
                continue
            dependencies = [tasks.get(item, {}) for item in task.get("depends_on") or []]
            if all(item.get("status") == "completed" for item in dependencies):
                result.append(dict(task))
        return result

    def transcript(self, session_id: str, *, include_acked: bool = True, limit: int = 100) -> list[AgentMessage]:
        payload = self._read(_safe(session_id, "session_id")) or {}
        result: list[AgentMessage] = []
        for item in (payload.get("messages") or [])[-max(1, min(500, int(limit))):]:
            if not include_acked and item.get("status") == "acked":
                continue
            try:
                result.append(_message_from_dict(item.get("message") or {}))
            except (TypeError, ValueError, AgentMessageError):
                continue
        return result

    def cancel(self, session_id: str, *, reason: str = "") -> dict[str, Any]:
        session_id = _safe(session_id, "session_id")
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = self._read(session_id)
            if payload is None:
                return {"session_id": session_id, "cancelled": False}
            payload["status"] = "cancelled"
            for item in payload.get("messages") or []:
                if item.get("status") in {"queued", "leased"}:
                    item["status"] = "cancelled"
            for task in (payload.get("tasks") or {}).values():
                if task.get("status") not in {"completed", "failed", "cancelled"}:
                    task["status"] = "cancelled"
            self._event(payload, "session_cancelled", reason=str(reason)[:500])
            self._write(session_id, payload)
            return {"session_id": session_id, "cancelled": True}
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def delegate_message(parent: AgentMessage, *, to_role: str, purpose: str, summary: str, source_event_id: str, key_id: str, signing_key: object) -> AgentMessage:
    """Create a bounded child message while preserving session and lineage."""
    if to_role not in AGENT_ROLES:
        raise MessageBusError("unknown delegation recipient")
    if parent.hop >= 2 or parent.round >= 3:
        raise MessageBusError("delegation budget exhausted")
    return build_message(
        session_id=parent.session_id,
        task_id=f"{parent.task_id}-{to_role}-{parent.round + 1}",
        from_role=parent.from_role,
        to=(to_role,),
        purpose=purpose,
        summary=summary,
        source_event_id=source_event_id,
        key_id=key_id,
        signing_key=signing_key,
        evidence_refs=parent.evidence_refs,
        hop=parent.hop + 1,
        round=parent.round + 1,
    )


__all__ = ["MessageBus", "MessageBusError", "SCHEMA", "TASK_SCHEMA", "delegate_message", "message_from_dict"]
