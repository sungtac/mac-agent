#!/usr/bin/env python3
"""Durable control-plane state for approval, cancellation, and recovery."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from edge_agent_agent_message import load_signing_key
from edge_agent_secure_paths import ensure_private_directory, open_lock, read_text


TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_CONTROL_STATES = frozenset({"running", "waiting_approval", "cancelled"})
MAX_TASKS = 8
MAX_EVENTS = 100


class ControlPlaneError(RuntimeError):
    pass


def is_cancel_request(text: str) -> bool:
    """Recognize the small, provider-neutral cancellation vocabulary."""
    normalized = " ".join(str(text or "").split()).casefold()
    return normalized in {"그만", "취소", "중단", "/cancel", "cancel", "stop"}


def _root() -> Path:
    return Path(os.environ.get("EDGE_AGENT_CONTROL_ROOT", str(Path.home() / ".edge-agent" / "state" / "control-plane"))).expanduser()


def _safe(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text or any(char in text for char in "/\\\x00"):
        raise ControlPlaneError(f"unsafe {name}")
    return text


def _bounded(value: object, limit: int = 1200) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _canonical_action(session_id: str, action: str, actor: str, source_event_id: str, created_epoch: float) -> bytes:
    return json.dumps(
        {"session_id": session_id, "action": action, "actor": actor, "source_event_id": source_event_id, "created_epoch": created_epoch},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sign_action(session_id: str, action: str, actor: str, source_event_id: str) -> dict[str, Any]:
    created_epoch = time.time()
    key_path = os.environ.get("EDGE_AGENT_MESSAGE_KEY_FILE", "").strip()
    if not key_path:
        raise ControlPlaneError("signed control action requires EDGE_AGENT_MESSAGE_KEY_FILE")
    key = load_signing_key(key_path)
    key_id = os.environ.get("EDGE_AGENT_MESSAGE_KEY_ID", "agent-message-v1").strip() or "agent-message-v1"
    signature = hmac.new(key, _canonical_action(session_id, action, actor, source_event_id, created_epoch), hashlib.sha256).hexdigest()
    return {
        "schema": "edge_agent.control_action.v1",
        "session_id": session_id,
        "action": action,
        "actor": _bounded(actor, 120),
        "source_event_id": _safe(source_event_id, "source_event_id"),
        "created_epoch": created_epoch,
        "key_id": key_id,
        "signature": signature,
    }


def verify_control_action(action: dict[str, Any], *, key_path: str | None = None) -> bool:
    if action.get("schema") != "edge_agent.control_action.v1":
        raise ControlPlaneError("unsupported control action schema")
    required = ("session_id", "action", "actor", "source_event_id", "created_epoch", "key_id", "signature")
    if any(key not in action for key in required):
        raise ControlPlaneError("incomplete control action")
    path = key_path or os.environ.get("EDGE_AGENT_MESSAGE_KEY_FILE", "").strip()
    if not path:
        raise ControlPlaneError("control action verification key is missing")
    key = load_signing_key(path)
    expected_id = os.environ.get("EDGE_AGENT_MESSAGE_KEY_ID", "agent-message-v1").strip() or "agent-message-v1"
    if action["key_id"] != expected_id:
        raise ControlPlaneError("control action key id mismatch")
    expected = hmac.new(
        key,
        _canonical_action(str(action["session_id"]), str(action["action"]), str(action["actor"]), str(action["source_event_id"]), float(action["created_epoch"])),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(str(action["signature"]), expected):
        raise ControlPlaneError("control action signature mismatch")
    return True


class ControlPlaneStore:
    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = ensure_private_directory(Path(root).expanduser() if root else _root())

    def _path(self, chat_id: object) -> Path:
        digest = hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()[:32]
        return self.root / f"chat-{digest}.json"

    @contextlib.contextmanager
    def _locked(self, chat_id: object) -> Iterator[Path]:
        path = self._path(chat_id)
        descriptor = open_lock(path.with_suffix(".lock"))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield path
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(read_text(path))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload.setdefault("schema", "edge_agent.control_plane.v1")
        payload.setdefault("status", "idle")
        payload.setdefault("generation", 0)
        payload.setdefault("tasks", {})
        payload.setdefault("events", [])
        if not isinstance(payload["tasks"], dict) or not isinstance(payload["events"], list):
            raise ControlPlaneError("control state is malformed")
        return payload

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".control-", dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _event(payload: dict[str, Any], event_type: str, *, action: dict[str, Any] | None = None, **extra: Any) -> None:
        event = {"event_type": event_type, "created_epoch": time.time(), **extra}
        if action is not None:
            verify_control_action(action)
            event["action"] = action
        events = list(payload.get("events") or [])
        events.append(event)
        payload["events"] = events[-MAX_EVENTS:]
        payload["updated_epoch"] = time.time()

    def start_task(self, chat_id: object, task_id: str, *, roles: tuple[str, ...] = ()) -> dict[str, Any]:
        task_id = _safe(task_id, "task_id")
        with self._locked(chat_id) as path:
            payload = self._read(path)
            tasks = payload["tasks"]
            if task_id not in tasks and len(tasks) >= MAX_TASKS:
                terminal = [
                    (existing_id, item)
                    for existing_id, item in tasks.items()
                    if item.get("status") in TERMINAL_TASK_STATES
                ]
                terminal.sort(
                    key=lambda pair: (
                        float(pair[1].get("finished_epoch") or pair[1].get("started_epoch") or 0),
                        pair[0],
                    )
                )
                while len(tasks) >= MAX_TASKS and terminal:
                    existing_id, _ = terminal.pop(0)
                    tasks.pop(existing_id, None)
                if len(tasks) >= MAX_TASKS:
                    raise ControlPlaneError("control plane task concurrency cap reached")
            tasks[task_id] = {"status": "running", "roles": list(roles), "started_epoch": time.time()}
            if payload.get("status") == "cancelled":
                payload["status"] = "running"
                payload["generation"] = int(payload.get("generation") or 0) + 1
            self._event(payload, "task_started", task_id=task_id)
            self._write(path, payload)
            return payload

    def request_approval(self, chat_id: object, task_id: str, approval_ref: str, *, actor: str = "telegram-user") -> dict[str, Any]:
        task_id, approval_ref = _safe(task_id, "task_id"), _safe(approval_ref, "approval_ref")
        with self._locked(chat_id) as path:
            payload = self._read(path)
            task = payload["tasks"].get(task_id)
            if not task:
                raise ControlPlaneError("approval task not found")
            action = _sign_action(str(chat_id), "request_approval", actor, f"{task_id}-approval")
            task.update({"status": "waiting_approval", "approval_ref": approval_ref})
            payload["status"] = "waiting_approval"
            self._event(payload, "approval_requested", action=action, task_id=task_id, approval_ref=approval_ref)
            self._write(path, payload)
            return payload

    def resolve_approval(self, chat_id: object, approval_ref: str, *, approved: bool, actor: str = "telegram-user") -> dict[str, Any]:
        approval_ref = _safe(approval_ref, "approval_ref")
        with self._locked(chat_id) as path:
            payload = self._read(path)
            matches = [(task_id, task) for task_id, task in payload["tasks"].items() if task.get("approval_ref") == approval_ref]
            if not matches:
                raise ControlPlaneError("approval reference not found")
            action = _sign_action(str(chat_id), "approve" if approved else "reject", actor, f"{approval_ref}-resolution")
            for task_id, task in matches:
                task["status"] = "running" if approved else "cancelled"
                task["approval_status"] = "approved" if approved else "rejected"
                self._event(payload, "approval_resolved", action=action, task_id=task_id, approved=approved)
            payload["status"] = "running" if approved else "cancelled"
            self._write(path, payload)
            return payload

    def cancel_chat(self, chat_id: object, *, reason: str, actor: str = "telegram-user") -> dict[str, Any]:
        with self._locked(chat_id) as path:
            payload = self._read(path)
            action = _sign_action(str(chat_id), "cancel", actor, f"cancel-{int(time.time() * 1000)}")
            payload["generation"] = int(payload.get("generation") or 0) + 1
            payload["status"] = "cancelled"
            payload["cancel_reason"] = _bounded(reason, 500)
            for task in payload["tasks"].values():
                if task.get("status") not in TERMINAL_TASK_STATES:
                    task["status"] = "cancelled"
                    task["cancelled_epoch"] = time.time()
            self._event(payload, "cascade_cancelled", action=action, reason=_bounded(reason, 500))
            self._write(path, payload)
            return payload

    def mark_task(self, chat_id: object, task_id: str, status: str, *, summary: str = "") -> dict[str, Any]:
        task_id = _safe(task_id, "task_id")
        if status not in TERMINAL_TASK_STATES | {"running", "waiting_approval"}:
            raise ControlPlaneError("unsupported task status")
        with self._locked(chat_id) as path:
            payload = self._read(path)
            task = payload["tasks"].get(task_id)
            if not task:
                raise ControlPlaneError("task not found")
            if task.get("status") == "cancelled" and status not in {"cancelled", "failed"}:
                raise ControlPlaneError("cancelled task cannot resume without approval")
            task.update({"status": status, "summary": _bounded(summary)})
            if status in TERMINAL_TASK_STATES:
                task["finished_epoch"] = time.time()
            active = [item for item in payload["tasks"].values() if item.get("status") not in TERMINAL_TASK_STATES]
            payload["status"] = "running" if active else "completed"
            self._event(payload, "task_updated", task_id=task_id, status=status)
            self._write(path, payload)
            return payload

    def snapshot(self, chat_id: object) -> dict[str, Any]:
        return self._read(self._path(chat_id))

    def is_cancelled(self, chat_id: object, task_id: str) -> bool:
        payload = self.snapshot(chat_id)
        task = payload.get("tasks", {}).get(str(task_id), {})
        return payload.get("status") == "cancelled" or task.get("status") == "cancelled"

    def recoverable(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.root.glob("chat-*.json")):
            payload = self._read(path)
            if payload.get("status") in ACTIVE_CONTROL_STATES:
                result.append(payload)
        return result


__all__ = [
    "ControlPlaneError",
    "ControlPlaneStore",
    "ACTIVE_CONTROL_STATES",
    "TERMINAL_TASK_STATES",
    "is_cancel_request",
    "verify_control_action",
]
