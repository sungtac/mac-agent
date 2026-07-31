#!/usr/bin/env python3
"""Shared logical-session contract for terminal and channel adapters.

This module is intentionally side-effect free.  It does not resume a native
provider session, acquire a lease, read a transcript, or execute a command.
Adapters use the contract to exchange a bounded handoff record while keeping
Claude, Codex, and Antigravity native sessions separate.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping


SCHEMA = "edge_agent.logical_session.v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SENSITIVE_MARKERS = (
    "token=",
    "api_key",
    "api key",
    "authorization:",
    "bearer ",
    "password=",
    "cookie:",
    "secret=",
    "private key",
)


class SessionStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    HANDOFF_READY = "handoff_ready"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SessionChannel(StrEnum):
    TERMINAL = "terminal"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    INTERNAL = "internal"


class Provider(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    ANTIGRAVITY = "antigravity"
    GEMMA = "gemma"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_logical_session_id() -> str:
    """Return a non-secret, human-searchable logical session identifier."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"sess-{stamp}-{uuid.uuid4().hex[:12]}"


def _require_id(name: str, value: str) -> str:
    value = str(value or "").strip()
    if not value or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{name} must be a non-empty safe identifier")
    return value


def _check_safe_text(name: str, value: str) -> str:
    value = str(value or "")
    lowered = value.lower()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise ValueError(f"{name} contains a sensitive marker")
    return value


@dataclass
class LogicalSession:
    """Provider-neutral handoff state shared by channel adapters.

    ``native_sessions`` maps a provider name to its own CLI session/thread
    identifier.  It is deliberately not a single shared provider session:
    native resume is only valid when the provider adapter confirms its own cwd,
    permissions, and session ownership constraints.
    """

    logical_session_id: str
    task_id: str
    channel: SessionChannel | str
    request_id: str = ""
    provider: Provider | str | None = None
    native_sessions: dict[str, str] = field(default_factory=dict)
    workspace: str = ""
    worktree: str = ""
    base_commit: str = ""
    owner: str = ""
    status: SessionStatus | str = SessionStatus.CREATED
    summary: str = ""
    next_action: str = ""
    risk_notes: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    approval_ref: str = ""
    lease_owner: str = ""
    lease_expires_at: str = ""
    updated_at: str = field(default_factory=_now)

    schema: str = field(default=SCHEMA, init=False)

    def __post_init__(self) -> None:
        self.logical_session_id = _require_id("logical_session_id", self.logical_session_id)
        self.task_id = _require_id("task_id", self.task_id)
        self.channel = SessionChannel(self.channel)
        if self.request_id:
            self.request_id = _require_id("request_id", self.request_id)
        if self.provider is not None:
            self.provider = Provider(self.provider)
        self.status = SessionStatus(self.status)
        self.owner = _check_safe_text("owner", self.owner)
        self.summary = _check_safe_text("summary", self.summary)
        self.next_action = _check_safe_text("next_action", self.next_action)
        self.risk_notes = [_check_safe_text("risk_note", item) for item in self.risk_notes]
        self.approval_ref = _check_safe_text("approval_ref", self.approval_ref)
        self._validate_native_sessions()
        self._validate_paths()
        if len(self.summary) > 8000:
            raise ValueError("summary exceeds the logical-session limit")
        if len(self.decisions) > 50:
            raise ValueError("too many session decisions")
        if len(self.risk_notes) > 50:
            raise ValueError("too many session risk notes")
        if len(self.changed_files) > 500:
            raise ValueError("too many changed files")

    def _validate_native_sessions(self) -> None:
        if not isinstance(self.native_sessions, dict):
            raise ValueError("native_sessions must be an object")
        for provider, session_id in self.native_sessions.items():
            Provider(provider)
            _check_safe_text(f"native_sessions[{provider}]", session_id)
            if not str(session_id).strip():
                raise ValueError(f"native_sessions[{provider}] must not be empty")

    def _validate_paths(self) -> None:
        for name, value in (("workspace", self.workspace), ("worktree", self.worktree)):
            if "\x00" in str(value):
                raise ValueError(f"{name} contains a NUL byte")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["channel"] = self.channel.value
        result["provider"] = self.provider.value if self.provider is not None else None
        result["status"] = self.status.value
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LogicalSession":
        if payload.get("schema") != SCHEMA:
            raise ValueError(f"unsupported session schema: {payload.get('schema')!r}")
        values = dict(payload)
        values.pop("schema", None)
        values.pop("schema_version", None)
        return cls(**values)

    def bind_native_session(self, provider: Provider | str, native_session_id: str) -> None:
        """Bind one provider's native session without merging provider history."""
        selected = Provider(provider)
        _check_safe_text("native_session_id", native_session_id)
        if not str(native_session_id).strip():
            raise ValueError("native_session_id must not be empty")
        self.native_sessions[selected.value] = str(native_session_id)
        self.provider = selected
        self.updated_at = _now()


def load_logical_session(payload: Mapping[str, Any]) -> LogicalSession:
    """Validate and load a serialized session without performing I/O."""
    return LogicalSession.from_dict(payload)
