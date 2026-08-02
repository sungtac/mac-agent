#!/usr/bin/env python3
"""Bounded Telegram context envelopes and short-lived entity anchors.

This module stores identifiers and small, sanitized references only.  It never
stores a transcript or provider credentials, and it does not resume a native
provider session.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from edge_agent_secure_paths import ensure_private_directory, open_lock, read_text, reject_symlink_components
from urllib.parse import urlsplit, urlunsplit

from edge_agent_session_contract import new_logical_session_id


SCHEMA_VERSION = "edge_agent.context_envelope.v1"
DEFAULT_CONTEXT_ROOT = Path.home() / ".edge-agent" / "state" / "telegram-context"
DEFAULT_ANCHOR_TTL_SECONDS = 900
DEFAULT_ANCHOR_RETENTION_SECONDS = 86400
DEFAULT_ANCHOR_CONFIDENCE = 0.35
MAX_TOPIC_CHARS = 240
MAX_ANCHORS = 50

_SENSITIVE_PATTERN = re.compile(
    r"(?:token|api[_ -]?key|password|bearer|cookie|secret|authorization|private[ _-]?key)\s*[:=]??\s*[^\s&]+",
    re.IGNORECASE,
)
_URL_PATTERN = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_FOLLOW_UP = ("팩트체크", "팩트 체크", "분석", "요약", "확인", "검토해줘", "검토 해줘")
_DEICTIC = ("이거", "그거", "저거", "이 영상", "그 영상", "이 내용", "그 내용", "이 링크", "방금")
_GREETING = ("안녕", "안녕하세요", "하이", "hello", "hi", "반가워")
_DIRECT_STATUS_QUERY = ("현재 상태", "서비스 상태", "실행 상태", "상태 확인", "정상인지", "헬스체크", "health check")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _timestamp(value: str | datetime | None) -> str:
    return (value if isinstance(value, str) else (value or datetime.now(timezone.utc))).isoformat() if isinstance(value, datetime) else (value or _now())


def _contains_sensitive(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_SENSITIVE_PATTERN.search(value))
    if isinstance(value, Mapping):
        return any(_contains_sensitive(k) or _contains_sensitive(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_sensitive(item) for item in value)
    return False


def _safe_component(value: str | int) -> str:
    value = str(value).strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise ValueError("unsafe context path component")
    return value


def _normalize_topic(value: str) -> str:
    normalized = " ".join(str(value).split())[:MAX_TOPIC_CHARS]
    if _contains_sensitive(normalized):
        return "[redacted]"
    return normalized


def _sanitize_url(value: str) -> str:
    if _contains_sensitive(value):
        return "[redacted]"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "[redacted]"
    if not parts.scheme or not parts.netloc:
        return _normalize_topic(value)
    # HTTP basic-auth userinfo is a credential even when it is not labelled
    # with a password/token key. Never persist or inject it into a provider.
    if parts.username is not None or parts.password is not None:
        return "[redacted]"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))[:MAX_TOPIC_CHARS]


@dataclass
class ContextEnvelope:
    schema_version: str
    channel: str
    provider: str
    chat_id: str
    message_id: str
    reply_to_message_id: str | None
    logical_session_id: str
    created_at: str
    anchor_reference: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported context envelope schema")
        for name in ("channel", "provider", "chat_id", "message_id", "logical_session_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ContextEnvelope":
        return cls(**dict(payload))


@dataclass
class EntityAnchor:
    chat_id: str
    source_message_id: str
    kind: str
    sanitized_value: str
    confidence: float
    created_at: str
    bound_message_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EntityAnchor":
        values = dict(payload)
        values.setdefault("bound_message_ids", [])
        return cls(**values)


@dataclass
class AnchorResolution:
    status: str
    anchor: EntityAnchor | None = None
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == "resolved"


@dataclass
class ContextPreparation:
    envelope: ContextEnvelope
    resolution: AnchorResolution
    prompt_block: str

    @property
    def guard_required(self) -> bool:
        return self.resolution.status in {"ambiguous", "stale"}


def looks_like_anaphoric_reference(text: str, recent_anchors: Iterable[EntityAnchor]) -> bool:
    """Recognize short follow-ups only when there is an anchor to follow."""
    normalized = " ".join(str(text or "").split()).lower()
    if not normalized or any(normalized.startswith(greeting) for greeting in _GREETING):
        return False
    # A direct status question is self-contained. It must not be interpreted
    # as a follow-up to the previous URL/topic anchor merely because it uses
    # the ordinary Korean word "확인".
    if any(phrase in normalized for phrase in _DIRECT_STATUS_QUERY):
        return False
    if not any(anchor for anchor in recent_anchors):
        return False
    return any(word in normalized for word in _DEICTIC + _FOLLOW_UP)


def extract_anchor(
    text: str,
    *,
    chat_id: str | int,
    source_message_id: str | int,
    now: str | datetime | None = None,
    confidence: float = DEFAULT_ANCHOR_CONFIDENCE,
) -> EntityAnchor | None:
    """Extract one bounded URL/topic anchor from an original user message."""
    raw = " ".join(str(text or "").split())
    if not raw:
        return None
    match = _URL_PATTERN.search(raw)
    if match:
        value = match.group(0).rstrip(".,!?)]}")
        return EntityAnchor(str(chat_id), str(source_message_id), "url", _sanitize_url(value), float(confidence), _timestamp(now))
    if any(raw.lower().startswith(greeting) for greeting in _GREETING):
        return None
    if any(word in raw.lower() for word in _FOLLOW_UP + _DEICTIC):
        return None
    topic = _normalize_topic(raw)
    if not topic:
        return None
    return EntityAnchor(str(chat_id), str(source_message_id), "topic", topic, float(confidence), _timestamp(now))


def _resolve(
    text: str,
    anchors: list[EntityAnchor],
    *,
    reply_to_message_id: str | int | None = None,
    now: str | datetime | None = None,
    ttl_seconds: int = DEFAULT_ANCHOR_TTL_SECONDS,
) -> AnchorResolution:
    current = _as_datetime(now or _now())
    reply = str(reply_to_message_id) if reply_to_message_id is not None else None
    if reply:
        for anchor in reversed(anchors):
            if reply == anchor.source_message_id or reply in anchor.bound_message_ids:
                return AnchorResolution("resolved", anchor, "explicit reply binding takes priority over TTL")
    valid: list[EntityAnchor] = []
    stale: list[EntityAnchor] = []
    for anchor in anchors:
        try:
            age = (current - _as_datetime(anchor.created_at)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            continue
        (valid if age <= ttl_seconds else stale).append(anchor)
    if not looks_like_anaphoric_reference(text, valid or stale):
        return AnchorResolution("none", None, "no explicit reply or follow-up reference")
    if len(valid) > 1:
        return AnchorResolution("ambiguous", None, "multiple valid anchors match the follow-up")
    if len(valid) == 1:
        return AnchorResolution("resolved", valid[0], "recent follow-up reference")
    if stale:
        return AnchorResolution("stale", stale[-1], "matching anchor exceeded TTL")
    return AnchorResolution("none", None, "no matching anchor")


def resolve_anchor(
    text: str,
    anchors: Iterable[EntityAnchor],
    *,
    reply_to_message_id: str | int | None = None,
    now: str | datetime | None = None,
    ttl_seconds: int = DEFAULT_ANCHOR_TTL_SECONDS,
) -> AnchorResolution:
    return _resolve(text, list(anchors), reply_to_message_id=reply_to_message_id, now=now, ttl_seconds=ttl_seconds)


class ContextEnvelopeStore:
    def __init__(self, root: str | Path | None = None, *, ttl_seconds: int = DEFAULT_ANCHOR_TTL_SECONDS, retention_seconds: int = DEFAULT_ANCHOR_RETENTION_SECONDS):
        self.root = Path(root or DEFAULT_CONTEXT_ROOT).expanduser()
        self.ttl_seconds = int(ttl_seconds)
        self.retention_seconds = int(retention_seconds)

    def _paths(self, channel: str, chat_id: str | int) -> tuple[Path, Path]:
        ensure_private_directory(self.root)
        component = _safe_component(str(chat_id).replace("-", "m"))
        channel_component = _safe_component(channel)
        record_root = ensure_private_directory(self.root / "records" / channel_component)
        lock_root = ensure_private_directory(self.root / "locks" / channel_component)
        return (record_root / f"{component}.json", lock_root / f"{component}.lock")

    @contextlib.contextmanager
    def _locked(self, channel: str, chat_id: str | int):
        _, lock_path = self._paths(channel, chat_id)
        fd = open_lock(lock_path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read_unlocked(self, channel: str, chat_id: str | int) -> dict[str, Any]:
        path, _ = self._paths(channel, chat_id)
        if not path.exists():
            return {"schema_version": SCHEMA_VERSION, "logical_session_id": new_logical_session_id(), "envelope": None, "anchors": []}
        payload = json.loads(read_text(path))
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported context record schema")
        payload.setdefault("anchors", [])
        return payload

    def _write_unlocked(self, channel: str, chat_id: str | int, payload: Mapping[str, Any]) -> None:
        path, _ = self._paths(channel, chat_id)
        ensure_private_directory(path.parent)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
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

    def _cleanup_unlocked(self, payload: dict[str, Any], now: str | datetime) -> None:
        current = _as_datetime(now)
        kept = []
        for item in payload.get("anchors", []):
            try:
                age = (current - _as_datetime(item["created_at"])).total_seconds()
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if age <= self.retention_seconds:
                kept.append(item)
        payload["anchors"] = kept[-MAX_ANCHORS:]

    def save_envelope(self, envelope: ContextEnvelope, anchor: EntityAnchor | None = None) -> bool:
        try:
            with self._locked(envelope.channel, envelope.chat_id):
                payload = self._read_unlocked(envelope.channel, envelope.chat_id)
                if anchor is not None:
                    if _contains_sensitive(anchor.to_dict()):
                        anchor = EntityAnchor(anchor.chat_id, anchor.source_message_id, "redacted", "[redacted]", 0.0, anchor.created_at)
                    payload["anchors"] = [item for item in payload.get("anchors", []) if item.get("source_message_id") != anchor.source_message_id]
                    payload["anchors"].append(anchor.to_dict())
                payload["envelope"] = envelope.to_dict()
                self._cleanup_unlocked(payload, envelope.created_at)
                self._write_unlocked(envelope.channel, envelope.chat_id, payload)
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def bind_response_message(self, *, channel: str, chat_id: str | int, source_message_id: str | int, response_message_id: str | int) -> bool:
        try:
            with self._locked(channel, chat_id):
                payload = self._read_unlocked(channel, chat_id)
                for item in payload.get("anchors", []):
                    if str(item.get("source_message_id")) == str(source_message_id):
                        ids = item.setdefault("bound_message_ids", [])
                        if str(response_message_id) not in ids:
                            ids.append(str(response_message_id))
                        self._write_unlocked(channel, chat_id, payload)
                        return True
            return False
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def anchors(self, *, channel: str, chat_id: str | int, now: str | datetime | None = None) -> list[EntityAnchor]:
        try:
            with self._locked(channel, chat_id):
                payload = self._read_unlocked(channel, chat_id)
                self._cleanup_unlocked(payload, now or _now())
                self._write_unlocked(channel, chat_id, payload)
                return [EntityAnchor.from_dict(item) for item in payload.get("anchors", [])]
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return []

    def resolve_anchor(self, *, channel: str, chat_id: str | int, text: str, reply_to_message_id: str | int | None = None, now: str | datetime | None = None) -> AnchorResolution:
        try:
            with self._locked(channel, chat_id):
                payload = self._read_unlocked(channel, chat_id)
                self._cleanup_unlocked(payload, now or _now())
                self._write_unlocked(channel, chat_id, payload)
                anchors = [EntityAnchor.from_dict(item) for item in payload.get("anchors", [])]
            return _resolve(text, anchors, reply_to_message_id=reply_to_message_id, now=now, ttl_seconds=self.ttl_seconds)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return AnchorResolution("unavailable", None, "context storage is unavailable")

    def logical_session_id(self, *, channel: str, chat_id: str | int) -> str:
        try:
            with self._locked(channel, chat_id):
                payload = self._read_unlocked(channel, chat_id)
                value = str(payload.get("logical_session_id") or new_logical_session_id())
                payload["logical_session_id"] = value
                self._write_unlocked(channel, chat_id, payload)
                return value
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return new_logical_session_id()

    def prepare(self, *, channel: str, provider: str, chat_id: str | int, message_id: str | int, reply_to_message_id: str | int | None, text: str, now: str | datetime | None = None) -> ContextPreparation:
        created = _timestamp(now)
        logical_id = self.logical_session_id(channel=channel, chat_id=chat_id)
        current_anchors = self.anchors(channel=channel, chat_id=chat_id, now=created)
        anchor = extract_anchor(text, chat_id=chat_id, source_message_id=message_id, now=created)
        envelope = ContextEnvelope(SCHEMA_VERSION, channel, provider, str(chat_id), str(message_id), str(reply_to_message_id) if reply_to_message_id is not None else None, logical_id, created, str(message_id) if anchor else None)
        self.save_envelope(envelope, anchor)
        resolution = AnchorResolution("resolved", anchor, "anchor extracted from current message") if anchor else self.resolve_anchor(channel=channel, chat_id=chat_id, text=text, reply_to_message_id=reply_to_message_id, now=created)
        return ContextPreparation(envelope, resolution, render_prompt_context(envelope, resolution))


def render_prompt_context(envelope: ContextEnvelope, resolution: AnchorResolution) -> str:
    lines = [
        "[Telegram context envelope]",
        f"schema_version: {envelope.schema_version}",
        f"channel: {envelope.channel}",
        f"provider: {envelope.provider}",
        f"chat_id: {envelope.chat_id}",
        f"message_id: {envelope.message_id}",
        f"reply_to_message_id: {envelope.reply_to_message_id or '(none)'}",
        f"logical_session_id: {envelope.logical_session_id}",
        f"anchor_resolution: {resolution.status}",
    ]
    if resolution.anchor is not None:
        lines.extend(["[sanitized anchor]", f"kind: {resolution.anchor.kind}", f"value: {resolution.anchor.sanitized_value}"])
    return "\n".join(lines)


def native_session_path(base: str | Path | None, *, role: str, chat_id: str | int | None = None) -> Path:
    root = Path(base).expanduser() if base else Path.home() / ".edge-agent" / "state" / "telegram-native-sessions" / f"{role}.json"
    reject_symlink_components(root)
    if chat_id is None:
        return root.resolve()
    safe_chat = re.sub(r"[^A-Za-z0-9_.-]", "_", str(chat_id))
    candidate = root.with_name(f"{root.stem}.chat-{safe_chat}{root.suffix or '.json'}")
    reject_symlink_components(candidate)
    return candidate.resolve()


def native_session_resume_allowed(metadata: Mapping[str, Any], *, chat_id: str | int, provider: str, workspace: str, workspace_identity: str | None = None) -> bool:
    if str(metadata.get("chat_id")) != str(chat_id) or str(metadata.get("provider")) != str(provider):
        return False
    if str(metadata.get("workspace")) != str(workspace):
        return False
    if workspace_identity is not None and str(metadata.get("workspace_identity")) != str(workspace_identity):
        return False
    return bool(str(metadata.get("session_id", "")).strip())
