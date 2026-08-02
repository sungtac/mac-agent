"""Stable, provider-neutral identity rules for Telegram ingress events.

This module is deliberately independent from Telegram, provider CLIs, and
runtime state.  It only accepts normalized ingress metadata and produces
canonical hashes.  Message content and routing decisions never participate in
the root task identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from typing import Any, Mapping, Sequence
import uuid


SCHEMA_VERSION = "edge_agent.task_identity.v1"
SHARED_CHAT_SCOPES = frozenset({"group", "supergroup", "channel", "shared_channel"})
PRIVATE_CHAT_SCOPES = frozenset({"private", "bot_private"})
_ROOT_IDENTITY_FIELDS = frozenset({
    "platform", "chat_scope", "message_id", "shared_chat_id", "bot_id", "chat_id",
})


class IdentityError(ValueError):
    """Raised when ingress identity data is incomplete or ambiguous."""


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically across Python processes and versions."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    """Return a SHA-256 hash of canonical JSON or bytes."""

    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def content_hash(value: str | bytes, *, secret_key: str | bytes | None = None) -> str:
    """Hash content for comparison; HMAC is available for sensitive material.

    A plain SHA-256 is suitable for non-sensitive deterministic test fixtures,
    but short or predictable messages should use ``secret_key`` to reduce
    dictionary-attack exposure.
    """

    payload = value if isinstance(value, bytes) else value.encode("utf-8")
    if secret_key is None:
        return hashlib.sha256(payload).hexdigest()
    key = secret_key if isinstance(secret_key, bytes) else secret_key.encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _text(value: Any, field: str) -> str:
    if value is None:
        raise IdentityError(f"{field} is required")
    result = str(value)
    if not result:
        raise IdentityError(f"{field} is empty")
    return result


def _message_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IdentityError("message_id must be a non-negative integer")
    return value


def immutable_ingress_identity(
    platform: str,
    chat_scope: str,
    *,
    message_id: int,
    shared_chat_id: str | int | None = None,
    bot_id: str | int | None = None,
    chat_id: str | int | None = None,
) -> dict[str, Any]:
    """Build the immutable identity payload for a Telegram ingress.

    Shared chats intentionally omit ``bot_id`` so multiple provider bots
    produce the same root task.  Private bot conversations include ``bot_id``
    and ``chat_id`` to prevent collisions between bot accounts.
    """

    platform_value = _text(platform, "platform")
    scope_value = _text(chat_scope, "chat_scope")
    payload: dict[str, Any] = {
        "platform": platform_value,
        "chat_scope": scope_value,
        "message_id": _message_id(message_id),
    }
    if scope_value in SHARED_CHAT_SCOPES:
        payload["shared_chat_id"] = _text(shared_chat_id, "shared_chat_id")
    elif scope_value in PRIVATE_CHAT_SCOPES:
        payload["bot_id"] = _text(bot_id, "bot_id")
        payload["chat_id"] = _text(chat_id, "chat_id")
    else:
        raise IdentityError(f"unsupported chat_scope: {scope_value}")
    return payload


def telegram_update_key(
    platform: str,
    *,
    bot_id: str | int,
    chat_id: str | int,
    update_id: int,
) -> str:
    """Identify duplicate delivery of one update to one Telegram bot."""

    payload = {
        "bot_id": _text(bot_id, "bot_id"),
        "chat_id": _text(chat_id, "chat_id"),
        "platform": _text(platform, "platform"),
        "update_id": _message_id(update_id),
    }
    return stable_hash(payload)


def cross_bot_message_key(
    platform: str,
    chat_scope: str,
    *,
    message_id: int,
    shared_chat_id: str | int | None = None,
    bot_id: str | int | None = None,
    chat_id: str | int | None = None,
) -> str:
    """Identify the same message across provider bots where applicable."""

    return stable_hash(
        immutable_ingress_identity(
            platform,
            chat_scope,
            message_id=message_id,
            shared_chat_id=shared_chat_id,
            bot_id=bot_id,
            chat_id=chat_id,
        )
    )


def root_task_id(**identity_fields: Any) -> str:
    """Return the permanent root task ID from immutable ingress fields only."""

    immutable_fields = {
        key: value for key, value in identity_fields.items() if key in _ROOT_IDENTITY_FIELDS
    }
    return stable_hash(immutable_ingress_identity(**immutable_fields))


def child_task_id(root_id: str, child_sequence: int, assigned_role: str) -> str:
    if not _text(root_id, "root_task_id"):
        raise IdentityError("root_task_id is required")
    if isinstance(child_sequence, bool) or not isinstance(child_sequence, int) or child_sequence < 0:
        raise IdentityError("child_sequence must be a non-negative integer")
    return stable_hash({
        "assigned_role": _text(assigned_role, "assigned_role"),
        "child_sequence": child_sequence,
        "root_task_id": root_id,
    })


def run_id(
    task_id: str,
    attempt_number: int,
    *,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Create a retry-specific run ID while retaining the task identity."""

    if isinstance(attempt_number, bool) or not isinstance(attempt_number, int) or attempt_number < 1:
        raise IdentityError("attempt_number must be a positive integer")
    if nonce is None and timestamp is None:
        nonce = uuid.uuid4().hex
    return stable_hash({
        "attempt_number": attempt_number,
        "nonce": nonce,
        "task_id": _text(task_id, "task_id"),
        "timestamp": timestamp,
    })


@dataclass(frozen=True)
class AttachmentIdentity:
    file_unique_id: str | None
    file_id_hash: str | None
    content_hash: str | None
    size: int | None
    mime_type: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_hash": self.content_hash,
            "file_id_hash": self.file_id_hash,
            "file_unique_id": self.file_unique_id,
            "mime_type": self.mime_type,
            "size": self.size,
        }


def attachment_identity(
    *,
    file_unique_id: str | None,
    file_id_hash: str | None,
    content_hash_value: str | None,
    size: int | None,
    mime_type: str | None,
) -> AttachmentIdentity:
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
        raise IdentityError("attachment size must be a non-negative integer or null")
    return AttachmentIdentity(
        file_unique_id=str(file_unique_id) if file_unique_id is not None else None,
        file_id_hash=str(file_id_hash) if file_id_hash is not None else None,
        content_hash=str(content_hash_value) if content_hash_value is not None else None,
        size=size,
        mime_type=str(mime_type) if mime_type is not None else None,
    )


def _attachment_dicts(attachments: Sequence[AttachmentIdentity | Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in attachments:
        result.append(item.as_dict() if isinstance(item, AttachmentIdentity) else dict(item))
    return sorted(result, key=canonical_json)


def attachment_change_status(
    previous: Sequence[AttachmentIdentity | Mapping[str, Any]],
    current: Sequence[AttachmentIdentity | Mapping[str, Any]],
) -> str:
    """Return SAME, CHANGED, or UNKNOWN without treating missing hashes as equal."""

    previous_items = _attachment_dicts(previous)
    current_items = _attachment_dicts(current)
    if not previous_items and not current_items:
        return "SAME"
    if any(item.get("content_hash") is None for item in previous_items + current_items):
        return "UNKNOWN"
    return "SAME" if previous_items == current_items else "CHANGED"


def revision_id(
    root_id: str,
    *,
    message_edit_version: int,
    body_hash: str | None,
    attachments: Sequence[AttachmentIdentity | Mapping[str, Any]],
) -> str:
    if isinstance(message_edit_version, bool) or not isinstance(message_edit_version, int) or message_edit_version < 0:
        raise IdentityError("message_edit_version must be a non-negative integer")
    return stable_hash({
        "attachments": _attachment_dicts(attachments),
        "body_hash": body_hash,
        "message_edit_version": message_edit_version,
        "root_task_id": _text(root_id, "root_task_id"),
    })
