#!/usr/bin/env python3
"""Signed, bounded internal messages between Edge Agent roles."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import threading
import tempfile
from typing import Any, Mapping, Sequence

import fcntl

from edge_agent_trace import TraceContext

SCHEMA = "edge_agent.agent_message.v1"
TRUST_DOMAIN = "telegram-internal"
AGENT_ROLES = frozenset({"claude", "codex", "antigravity", "roda"})
MAX_HOP = 2
MAX_ROUND = 3
MAX_SUMMARY_CHARS = 1600
MAX_EVIDENCE_REFS = 20
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SENSITIVE = re.compile(r"(?i)(?:token|api[_ -]?key|authorization|bearer|password|cookie|secret|private[_ -]?key)\s*[:=]")


class AgentMessageError(ValueError):
    """Raised when an internal message cannot be trusted or bounded."""


def _id(name: str, value: object) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID.fullmatch(text):
        raise AgentMessageError(f"{name} must be a safe non-empty identifier")
    return text


def _safe_text(name: str, value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise AgentMessageError(f"{name} exceeds {limit} characters")
    if _SENSITIVE.search(text):
        raise AgentMessageError(f"{name} contains a sensitive marker")
    return text


def _key(value: str | bytes | Mapping[str, str | bytes] | None, key_id: str | None = None) -> bytes:
    if value is None:
        raise AgentMessageError("message signing key is required")
    if isinstance(value, Mapping):
        if not key_id or key_id not in value:
            raise AgentMessageError("message key id is not available in keyring")
        value = value[key_id]
    key = value if isinstance(value, bytes) else str(value).encode("utf-8")
    if len(key) < 16:
        raise AgentMessageError("message signing key is too short")
    return key


@dataclass(frozen=True)
class AgentMessage:
    session_id: str
    task_id: str
    from_role: str
    to: tuple[str, ...]
    purpose: str
    summary: str
    evidence_refs: tuple[str, ...] = ()
    hop: int = 0
    round: int = 1
    source_event_id: str = ""
    trust_domain: str = TRUST_DOMAIN
    key_id: str = ""
    signature: str = ""
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    requires_user_report: bool = False
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _id("session_id", self.session_id))
        object.__setattr__(self, "task_id", _id("task_id", self.task_id))
        if self.from_role not in AGENT_ROLES:
            raise AgentMessageError(f"unknown sender role: {self.from_role}")
        targets = tuple(dict.fromkeys(str(item).strip() for item in self.to if str(item).strip()))
        if not targets or any(item not in AGENT_ROLES for item in targets):
            raise AgentMessageError("to must contain known agent roles")
        object.__setattr__(self, "to", targets)
        object.__setattr__(self, "purpose", _id("purpose", self.purpose))
        object.__setattr__(self, "summary", _safe_text("summary", self.summary, MAX_SUMMARY_CHARS))
        refs = tuple(str(item).strip() for item in self.evidence_refs if str(item).strip())
        if len(refs) > MAX_EVIDENCE_REFS or any(len(item) > 300 for item in refs):
            raise AgentMessageError("evidence_refs exceeds its bound")
        if any(_SENSITIVE.search(item) for item in refs):
            raise AgentMessageError("evidence_refs contains a sensitive marker")
        object.__setattr__(self, "evidence_refs", refs)
        if isinstance(self.hop, bool) or not 0 <= int(self.hop) <= MAX_HOP:
            raise AgentMessageError(f"hop must be between 0 and {MAX_HOP}")
        if isinstance(self.round, bool) or not 1 <= int(self.round) <= MAX_ROUND:
            raise AgentMessageError(f"round must be between 1 and {MAX_ROUND}")
        if self.trust_domain != TRUST_DOMAIN:
            raise AgentMessageError("untrusted message domain")
        object.__setattr__(self, "key_id", _id("key_id", self.key_id))
        object.__setattr__(self, "source_event_id", _id("source_event_id", self.source_event_id))
        if self.trace_id and self.span_id:
            trace = TraceContext(
                trace_id=self.trace_id,
                span_id=self.span_id,
                parent_span_id=self.parent_span_id,
            )
        else:
            trace = TraceContext.for_message(
                session_id=self.session_id,
                task_id=self.task_id,
                source_event_id=self.source_event_id,
                from_role=self.from_role,
                purpose=self.purpose,
                round_number=self.round,
                trace_id=self.trace_id,
                parent_span_id=self.parent_span_id,
            )
        object.__setattr__(self, "trace_id", trace.trace_id)
        object.__setattr__(self, "span_id", trace.span_id)
        object.__setattr__(self, "parent_span_id", trace.parent_span_id)
        if self.schema != SCHEMA:
            raise AgentMessageError("unsupported agent message schema")

    def unsigned_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature", None)
        return payload

    def canonical_bytes(self) -> bytes:
        return json.dumps(self.unsigned_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["to"] = list(self.to)
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload


def sign_message(message: AgentMessage, signing_key: str | bytes | Mapping[str, str | bytes]) -> AgentMessage:
    if hasattr(signing_key, "sign") and hasattr(signing_key, "key_id"):
        if message.key_id != signing_key.key_id:
            message = replace(message, key_id=signing_key.key_id, signature="")
        return replace(message, signature=signing_key.sign(message.canonical_bytes()))
    signature = hmac.new(_key(signing_key, message.key_id), message.canonical_bytes(), hashlib.sha256).hexdigest()
    return replace(message, signature=signature)


def verify_message(message: AgentMessage, signing_key: str | bytes | Mapping[str, str | bytes], *, expected_trust_domain: str = TRUST_DOMAIN, expected_key_id: str | None = None) -> bool:
    if message.trust_domain != expected_trust_domain:
        raise AgentMessageError("agent message trust domain mismatch")
    if expected_key_id is not None and message.key_id != expected_key_id:
        raise AgentMessageError("agent message key id mismatch")
    if not message.signature:
        raise AgentMessageError("unsigned agent message")
    if hasattr(signing_key, "verify") and hasattr(signing_key, "key_id"):
        if message.key_id != signing_key.key_id:
            raise AgentMessageError("agent identity key id mismatch")
        try:
            return bool(signing_key.verify(message.canonical_bytes(), message.signature))
        except Exception as exc:
            raise AgentMessageError("agent identity verification failed") from exc
    expected = hmac.new(_key(signing_key, message.key_id), message.canonical_bytes(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(message.signature, expected):
        raise AgentMessageError("agent message signature mismatch")
    return True


def build_message(*, session_id: str, task_id: str, from_role: str, to: Sequence[str], purpose: str, summary: str, source_event_id: str, key_id: str, signing_key: str | bytes | Mapping[str, str | bytes], evidence_refs: Sequence[str] = (), hop: int = 0, round: int = 1, trace_id: str = "", parent_span_id: str = "", requires_user_report: bool = False) -> AgentMessage:
    message = AgentMessage(session_id=session_id, task_id=task_id, from_role=from_role, to=tuple(to), purpose=purpose, summary=summary, evidence_refs=tuple(evidence_refs), hop=hop, round=round, source_event_id=source_event_id, key_id=key_id, trace_id=trace_id, parent_span_id=parent_span_id, requires_user_report=requires_user_report)
    return sign_message(message, signing_key)


def deduplication_key(message: AgentMessage) -> str:
    value = "|".join((message.source_event_id, message.from_role, message.purpose, str(message.round)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def next_hop(message: AgentMessage, *, recipient: str, signing_key: str | bytes | Mapping[str, str | bytes]) -> AgentMessage:
    if recipient not in AGENT_ROLES:
        raise AgentMessageError("unknown recipient role")
    if message.hop >= MAX_HOP:
        raise AgentMessageError("agent message hop limit reached")
    forwarded = replace(
        message,
        from_role=recipient,
        to=(recipient,),
        hop=message.hop + 1,
        parent_span_id=message.span_id,
        span_id="",
        signature="",
    )
    return sign_message(forwarded, signing_key)


def load_signing_key(path: str | os.PathLike[str]) -> bytes:
    key_path = Path(path).expanduser()
    if key_path.is_symlink() or not key_path.is_file():
        raise AgentMessageError("agent message signing key file is unavailable")
    if key_path.stat().st_mode & 0o077:
        raise AgentMessageError("agent message signing key file permissions are unsafe")
    return _key(key_path.read_bytes().strip())


def load_verification_keys(path: str | os.PathLike[str]) -> dict[str, bytes]:
    """Load an overlapping verification key set for safe key rotation."""
    root = Path(path).expanduser()
    if root.is_file():
        return {"agent-message-v1": load_signing_key(root)}
    if root.is_symlink() or not root.is_dir():
        raise AgentMessageError("agent message keyring is unavailable")
    result: dict[str, bytes] = {}
    for key_path in sorted(root.glob("*.key")):
        if key_path.is_symlink() or not key_path.is_file():
            continue
        result[key_path.stem] = load_signing_key(key_path)
    if not result:
        raise AgentMessageError("agent message keyring is empty")
    return result


class AgentMessageKeyring:
    """Small private keyring supporting overlap during active-key rotation."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser()
        if self.root.exists() and (self.root.is_symlink() or not self.root.is_dir()):
            raise AgentMessageError("agent message keyring path is unsafe")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def add_key(self, key_id: str, key: str | bytes, *, active: bool = True) -> None:
        _id("key_id", key_id)
        value = _key(key)
        target = self.root / f"{key_id}.key"
        descriptor, temporary = tempfile.mkstemp(prefix=".key-", dir=self.root)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600)
            if active:
                active_path = self.root / ".active"
                active_path.write_text(key_id, encoding="utf-8")
                os.chmod(active_path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def active_key_id(self) -> str:
        try:
            key_id = (self.root / ".active").read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            key_id = ""
        if not key_id:
            raise AgentMessageError("active agent message key is not configured")
        _id("key_id", key_id)
        return key_id

    def active_key(self) -> tuple[str, bytes]:
        key_id = self.active_key_id()
        return key_id, load_signing_key(self.root / f"{key_id}.key")

    def verification_keys(self) -> dict[str, bytes]:
        return load_verification_keys(self.root)


class AgentMessageDedupStore:
    """Dispatch guard with optional cross-process durable backing."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._path = Path(path).expanduser() if path is not None else None
        if self._path is not None:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _accept_persistent(self, key: str) -> bool:
        assert self._path is not None
        lock_path = self._path.with_name(self._path.name + ".lock")
        lock_path.touch(mode=0o600, exist_ok=True)
        with lock_path.open("r+", encoding="utf-8") as lock_stream:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    values = json.loads(self._path.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    values = []
                seen = {str(item) for item in values} if isinstance(values, list) else set()
                if key in seen:
                    return False
                seen.add(key)
                bounded = list(seen)[-10000:]
                descriptor, temporary = tempfile.mkstemp(prefix=".dedup-", dir=self._path.parent)
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                        json.dump(bounded, stream, ensure_ascii=False, separators=(",", ":"))
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self._path)
                    os.chmod(self._path, 0o600)
                finally:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
                return True
            finally:
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)

    def accept(self, message: AgentMessage, signing_key: str | bytes | Mapping[str, str | bytes], *, expected_key_id: str | None = None) -> bool:
        verify_message(message, signing_key, expected_key_id=expected_key_id)
        key = deduplication_key(message)
        with self._lock:
            if key in self._seen:
                return False
            if self._path is not None and not self._accept_persistent(key):
                self._seen.add(key)
                return False
            self._seen.add(key)
            return True


__all__ = ["AGENT_ROLES", "AgentMessage", "AgentMessageDedupStore", "AgentMessageError", "AgentMessageKeyring", "MAX_HOP", "MAX_ROUND", "build_message", "deduplication_key", "load_signing_key", "load_verification_keys", "next_hop", "sign_message", "verify_message"]
