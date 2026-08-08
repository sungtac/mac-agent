"""Bounded, scoped HMAC capability tokens for local agent actions.

This is a transition primitive: it provides explicit capability scoping while
the runtime still uses its existing HMAC keyring. It is intentionally not
presented as an Ed25519 identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os
import re
import time
from typing import Any, Mapping, Sequence


SCHEMA = "edge_agent.capability_token.v1"
MAX_LIFETIME_SECONDS = 3600.0
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class CapabilityError(ValueError):
    """A capability token is malformed, expired, or out of scope."""


def _id(name: str, value: object) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID.fullmatch(text):
        raise CapabilityError(f"{name} must be a safe non-empty identifier")
    return text


def _key(value: str | bytes | Mapping[str, str | bytes], key_id: str) -> bytes:
    selected: str | bytes | None = value
    if isinstance(value, Mapping):
        selected = value.get(key_id)
    if selected is None:
        raise CapabilityError("capability key id is unavailable")
    material = selected if isinstance(selected, bytes) else str(selected).encode("utf-8")
    if len(material) < 16:
        raise CapabilityError("capability key is too short")
    return material


def _canonical(payload: Mapping[str, Any]) -> bytes:
    values = dict(payload)
    values.pop("signature", None)
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class CapabilityToken:
    subject: str
    audience: str
    task_id: str
    actions: tuple[str, ...]
    issued_epoch: float
    expires_epoch: float
    nonce: str
    key_id: str
    signature: str = ""
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "subject", _id("subject", self.subject))
        object.__setattr__(self, "audience", _id("audience", self.audience))
        object.__setattr__(self, "task_id", _id("task_id", self.task_id))
        actions = tuple(sorted({_id("action", action) for action in self.actions}))
        if not actions:
            raise CapabilityError("capability actions must not be empty")
        object.__setattr__(self, "actions", actions)
        issued = float(self.issued_epoch)
        expires = float(self.expires_epoch)
        if not math.isfinite(issued) or not math.isfinite(expires) or expires <= issued:
            raise CapabilityError("invalid capability lifetime")
        if expires - issued > MAX_LIFETIME_SECONDS:
            raise CapabilityError("capability lifetime exceeds hard cap")
        object.__setattr__(self, "issued_epoch", issued)
        object.__setattr__(self, "expires_epoch", expires)
        object.__setattr__(self, "nonce", _id("nonce", self.nonce))
        object.__setattr__(self, "key_id", _id("key_id", self.key_id))
        if self.signature and not re.fullmatch(r"[0-9a-f]{64}", self.signature):
            raise CapabilityError("invalid capability signature")
        if self.schema != SCHEMA:
            raise CapabilityError("unsupported capability schema")

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "subject": self.subject,
            "audience": self.audience,
            "task_id": self.task_id,
            "actions": list(self.actions),
            "issued_epoch": self.issued_epoch,
            "expires_epoch": self.expires_epoch,
            "nonce": self.nonce,
            "key_id": self.key_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "signature": self.signature}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CapabilityToken":
        if payload.get("schema") != SCHEMA:
            raise CapabilityError("unsupported capability schema")
        values = dict(payload)
        values.pop("schema", None)
        return cls(**values)


def mint_capability(
    *,
    subject: str,
    audience: str,
    task_id: str,
    actions: Sequence[str],
    key_id: str,
    signing_key: str | bytes | Mapping[str, str | bytes],
    now: float | None = None,
    ttl_seconds: float = 300.0,
    nonce: str | None = None,
) -> CapabilityToken:
    issued = time.time() if now is None else float(now)
    if ttl_seconds <= 0 or ttl_seconds > MAX_LIFETIME_SECONDS:
        raise CapabilityError("capability ttl exceeds hard cap")
    token = CapabilityToken(
        subject=subject,
        audience=audience,
        task_id=task_id,
        actions=tuple(actions),
        issued_epoch=issued,
        expires_epoch=issued + float(ttl_seconds),
        nonce=nonce or f"nonce-{os.urandom(8).hex()}",
        key_id=key_id,
    )
    signature = hmac.new(_key(signing_key, token.key_id), _canonical(token.unsigned_dict()), hashlib.sha256).hexdigest()
    return CapabilityToken(**{**token.to_dict(), "signature": signature})


def verify_capability(
    token: CapabilityToken | Mapping[str, Any],
    verification_key: str | bytes | Mapping[str, str | bytes],
    *,
    required_action: str,
    subject: str,
    audience: str,
    task_id: str,
    now: float | None = None,
) -> bool:
    selected = token if isinstance(token, CapabilityToken) else CapabilityToken.from_dict(token)
    observed = time.time() if now is None else float(now)
    if not selected.signature:
        raise CapabilityError("unsigned capability")
    if not selected.issued_epoch <= observed < selected.expires_epoch:
        raise CapabilityError("capability expired or not yet valid")
    if _id("action", required_action) not in selected.actions:
        raise CapabilityError("capability action is not granted")
    if selected.subject != _id("subject", subject):
        raise CapabilityError("capability subject mismatch")
    if selected.audience != _id("audience", audience):
        raise CapabilityError("capability audience mismatch")
    if selected.task_id != _id("task_id", task_id):
        raise CapabilityError("capability task mismatch")
    expected = hmac.new(_key(verification_key, selected.key_id), _canonical(selected.unsigned_dict()), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(selected.signature, expected):
        raise CapabilityError("capability signature mismatch")
    return True


__all__ = ["CapabilityError", "CapabilityToken", "MAX_LIFETIME_SECONDS", "SCHEMA", "mint_capability", "verify_capability"]
