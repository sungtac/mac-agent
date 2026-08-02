"""Small HMAC provenance primitive shared by local lease/claim metadata."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from edge_agent_agent_message import load_signing_key, load_verification_keys

SCHEMA = "edge_agent.provenance.v1"


def _key_path(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    value = explicit or os.environ.get("EDGE_AGENT_PROVENANCE_KEY_FILE", "").strip() or os.environ.get("EDGE_AGENT_MESSAGE_KEY_FILE", "").strip()
    return Path(value).expanduser() if value else None


def _key_id(explicit: str | None = None) -> str:
    return explicit or os.environ.get("EDGE_AGENT_MESSAGE_KEY_ID", "agent-message-v1").strip() or "agent-message-v1"


def _canonical(kind: str, payload: dict[str, Any]) -> bytes:
    normalized = dict(payload)
    normalized.pop("signature", None)
    return json.dumps({"schema": SCHEMA, "kind": str(kind), "payload": normalized}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_provenance(kind: str, payload: dict[str, Any], *, key_path: str | os.PathLike[str] | None = None, key_id: str | None = None) -> tuple[str, str]:
    path = _key_path(key_path)
    if path is None:
        return "", ""
    resolved_id = _key_id(key_id)
    return resolved_id, hmac.new(load_signing_key(path), _canonical(kind, payload), hashlib.sha256).hexdigest()


def verify_provenance(kind: str, payload: dict[str, Any], *, key_id: str, signature: str, key_path: str | os.PathLike[str] | None = None) -> bool:
    path = _key_path(key_path)
    if path is None or not signature:
        raise ValueError("signed provenance requires a key and signature")
    keys = load_verification_keys(path) if path.is_dir() else {str(key_id): load_signing_key(path)}
    key = keys.get(str(key_id))
    if key is None:
        raise ValueError("provenance key id is unavailable")
    expected = hmac.new(key, _canonical(kind, payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(signature), expected):
        raise ValueError("provenance signature mismatch")
    return True


__all__ = ["SCHEMA", "sign_provenance", "verify_provenance"]
