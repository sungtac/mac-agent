#!/usr/bin/env python3
"""Durable, bounded Telegram response delivery outbox.

Provider execution and Telegram delivery are separate state transitions. This
module stores only the bounded response needed to retry delivery; it never
reruns a provider. Files are private, atomic, and expire when queried after
their retention deadline.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from edge_agent_secure_paths import ensure_private_directory, read_text


SCHEMA = "edge_agent.telegram_delivery.v1"
DEFAULT_ROOT = Path(
    os.environ.get(
        "EDGE_AGENT_TELEGRAM_DELIVERY_ROOT",
        str(Path.home() / ".edge-agent" / "state" / "telegram-delivery"),
    )
).expanduser()
DEFAULT_TTL_SECONDS = max(
    300,
    int(os.environ.get("EDGE_AGENT_TELEGRAM_DELIVERY_TTL_SECONDS", str(48 * 3600))),
)
MAX_REPLY_CHARS = 60_000


class DeliveryStoreError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _safe_id(value: object) -> str:
    text = str(value or "").strip()
    if not text or any(char in text for char in "/\\\x00"):
        raise DeliveryStoreError("delivery id must be a safe non-empty value")
    return text


def _root(root: str | Path | None = None) -> Path:
    configured = os.environ.get("EDGE_AGENT_TELEGRAM_DELIVERY_ROOT") if root is None else None
    return ensure_private_directory(Path(root or configured or DEFAULT_ROOT).expanduser())


def _path(delivery_id: str, root: str | Path | None = None) -> Path:
    return _root(root) / f"{_safe_id(delivery_id)}.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    ensure_private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".delivery-", dir=path.parent)
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


def _read(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(read_text(path))
    except FileNotFoundError:
        raise DeliveryStoreError(f"delivery record not found: {path.stem}") from None
    except (OSError, UnicodeError, RuntimeError, json.JSONDecodeError) as exc:
        raise DeliveryStoreError(f"delivery record is corrupt: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise DeliveryStoreError(f"unsupported delivery schema: {path}")
    if payload.get("status") not in {"pending", "succeeded", "expired"}:
        raise DeliveryStoreError(f"invalid delivery status: {path}")
    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or not all(isinstance(chunk, str) for chunk in chunks):
        raise DeliveryStoreError(f"invalid delivery chunks: {path}")
    if sum(len(chunk) for chunk in chunks) > MAX_REPLY_CHARS:
        raise DeliveryStoreError(f"delivery response exceeds bound: {path}")
    return payload


def create_delivery(
    *,
    task_id: str,
    role: str,
    chat_id: object,
    owner_user_id: object,
    source_message_id: object,
    session_id: str | None,
    request_preview: str,
    chunks: list[str],
    workspace: str = "",
    progress_message_id: object | None = None,
    delivery_id: str | None = None,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    delivery_id = _safe_id(delivery_id or f"{task_id}-{source_message_id or 'response'}")
    if not chunks or not all(isinstance(chunk, str) for chunk in chunks):
        raise DeliveryStoreError("delivery requires one or more text chunks")
    if sum(len(chunk) for chunk in chunks) > MAX_REPLY_CHARS:
        raise DeliveryStoreError("delivery response exceeds bound")
    created = now or _now()
    payload = {
        "schema": SCHEMA,
        "delivery_id": delivery_id,
        "task_id": str(task_id),
        "role": str(role),
        "chat_id": str(chat_id),
        "owner_user_id": str(owner_user_id or ""),
        "source_message_id": str(source_message_id or ""),
        "session_id": str(session_id or ""),
        "request_preview": str(request_preview or "")[:1000],
        "workspace": str(workspace or ""),
        "chunks": list(chunks),
        "progress_message_id": str(progress_message_id or ""),
        "sent_message_ids": {},
        "status": "pending",
        "retry_count": 0,
        "last_error": "",
        "created_at": _iso(created),
        "updated_at": _iso(created),
        "expires_at": _iso(created + timedelta(seconds=max(300, int(ttl_seconds)))),
    }
    target = _path(delivery_id, root)
    if target.exists():
        existing = _read(target)
        if existing.get("task_id") != str(task_id):
            raise DeliveryStoreError("delivery id collision")
        return existing
    _atomic_write(target, payload)
    return payload


def load_delivery(delivery_id: str, *, root: str | Path | None = None) -> dict[str, Any]:
    payload = _read(_path(delivery_id, root))
    try:
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DeliveryStoreError("delivery expiry is invalid") from exc
    if payload.get("status") == "pending" and _now() >= expires_at:
        payload["status"] = "expired"
        payload["updated_at"] = _iso(_now())
        _atomic_write(_path(delivery_id, root), payload)
    return payload


def list_pending_deliveries(
    *,
    chat_id: object,
    role: str,
    owner_user_id: object,
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    directory = _root(root)
    if not directory.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json"), key=lambda item: item.lstat().st_mtime):
        payload = load_delivery(path.stem, root=directory)
        if payload.get("status") != "pending":
            continue
        if (
            payload.get("chat_id") == str(chat_id)
            and payload.get("role") == str(role)
            and payload.get("owner_user_id") == str(owner_user_id or "")
        ):
            result.append(payload)
    return result


def pending_indexes(payload: dict[str, Any]) -> list[int]:
    sent = payload.get("sent_message_ids", {})
    return [index for index in range(len(payload["chunks"])) if str(index) not in sent]


def mark_chunk_sent(
    delivery_id: str,
    index: int,
    message_id: object,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    payload = load_delivery(delivery_id, root=root)
    if payload["status"] != "pending":
        return payload
    if index < 0 or index >= len(payload["chunks"]):
        raise DeliveryStoreError("delivery chunk index is out of range")
    sent = dict(payload.get("sent_message_ids") or {})
    sent.setdefault(str(index), str(message_id or ""))
    payload["sent_message_ids"] = sent
    payload["updated_at"] = _iso(_now())
    _atomic_write(_path(delivery_id, root), payload)
    return payload


def mark_delivery_succeeded(delivery_id: str, *, root: str | Path | None = None) -> dict[str, Any]:
    payload = load_delivery(delivery_id, root=root)
    if payload["status"] == "expired":
        raise DeliveryStoreError("expired delivery cannot succeed")
    if pending_indexes(payload):
        raise DeliveryStoreError("delivery still has unsent chunks")
    payload["status"] = "succeeded"
    payload["updated_at"] = _iso(_now())
    _atomic_write(_path(delivery_id, root), payload)
    return payload


def mark_delivery_failed(
    delivery_id: str,
    error: object,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    payload = load_delivery(delivery_id, root=root)
    if payload["status"] != "pending":
        return payload
    payload["retry_count"] = int(payload.get("retry_count", 0)) + 1
    payload["last_error"] = str(error)[-1000:]
    payload["updated_at"] = _iso(_now())
    _atomic_write(_path(delivery_id, root), payload)
    return payload
