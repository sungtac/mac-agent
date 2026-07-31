#!/usr/bin/env python3
"""Minimal Team OS ↔ Edge Agent request/result DTO contract.

The adapter boundary is deliberately data-only.  It does not import Team OS
routers, execute a provider, or grant approval.  Approval and execution remain
owned by their respective layers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from edge_agent_session_contract import _check_safe_text, _require_id


REQUEST_SCHEMA = "team_os.edge_agent_request.v1"
RESULT_SCHEMA = "edge_agent.team_os_result.v1"
_RISK_LEVELS = {"read", "draft", "write", "send", "delete", "system", "secret"}
_RESULT_STATUSES = {"queued", "running", "passed", "failed", "cancelled", "rate_limited", "blocked"}


class AdapterSource(StrEnum):
    TEAM_OS = "team_os"
    TERMINAL = "terminal"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    INTERNAL = "internal"


def _bounded_list(name: str, values: list[str] | tuple[str, ...], limit: int) -> tuple[str, ...]:
    result = tuple(_check_safe_text(name, value).strip() for value in values if str(value).strip())
    if len(result) > limit:
        raise ValueError(f"{name} exceeds {limit} entries")
    return result


@dataclass(frozen=True)
class EdgeAgentRequest:
    request_id: str
    task_id: str
    logical_session_id: str
    objective: str
    source: AdapterSource | str
    risk_level: str = "read"
    allowed_files: tuple[str, ...] = ()
    workspace_or_worktree: str = ""
    base_commit: str = ""
    required_outputs: tuple[str, ...] = ()
    completion_gates: tuple[str, ...] = ()
    approval_ref: str = ""
    dispatch_allowed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    schema: str = field(default=REQUEST_SCHEMA, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _require_id("request_id", self.request_id))
        object.__setattr__(self, "task_id", _require_id("task_id", self.task_id))
        object.__setattr__(self, "logical_session_id", _require_id("logical_session_id", self.logical_session_id))
        object.__setattr__(self, "source", AdapterSource(self.source))
        objective = _check_safe_text("objective", self.objective).strip()
        if not objective or len(objective) > 4000:
            raise ValueError("objective must be 1-4000 characters")
        object.__setattr__(self, "objective", objective)
        risk = str(self.risk_level).lower().strip()
        if risk not in _RISK_LEVELS:
            raise ValueError(f"unsupported risk_level: {self.risk_level!r}")
        object.__setattr__(self, "risk_level", risk)
        object.__setattr__(self, "allowed_files", _bounded_list("allowed_files", self.allowed_files, 200))
        object.__setattr__(self, "required_outputs", _bounded_list("required_outputs", self.required_outputs, 50))
        object.__setattr__(self, "completion_gates", _bounded_list("completion_gates", self.completion_gates, 50))
        object.__setattr__(self, "workspace_or_worktree", _check_safe_text("workspace_or_worktree", self.workspace_or_worktree))
        object.__setattr__(self, "base_commit", _check_safe_text("base_commit", self.base_commit))
        object.__setattr__(self, "approval_ref", _check_safe_text("approval_ref", self.approval_ref))
        for key in self.metadata:
            key_text = str(key).strip()
            if not key_text:
                raise ValueError("metadata keys must not be empty")
            _check_safe_text("metadata key", key_text)
        if self.dispatch_allowed and self.risk_level in {"send", "delete", "system", "secret"} and not self.approval_ref:
            raise ValueError("high-risk dispatch requires approval_ref")
        if self.risk_level == "secret" and self.dispatch_allowed:
            raise ValueError("secret requests cannot be dispatch_allowed")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source"] = self.source.value
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EdgeAgentRequest":
        if payload.get("schema") != REQUEST_SCHEMA:
            raise ValueError(f"unsupported request schema: {payload.get('schema')!r}")
        values = dict(payload)
        values.pop("schema", None)
        return cls(**values)


@dataclass(frozen=True)
class EdgeAgentResult:
    request_id: str
    task_id: str
    logical_session_id: str
    status: str
    execution_status: str = ""
    integration_status: str = ""
    commit: str = ""
    changed_files: tuple[str, ...] = ()
    verification_tier: str = ""
    event_idempotency_key: str = ""
    provider: str = ""
    usage_snapshot_ref: str = ""
    evidence_refs: tuple[str, ...] = ()
    error_code: str = ""
    next_action: str = ""
    rollback_ref: str = ""
    schema: str = field(default=RESULT_SCHEMA, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _require_id("request_id", self.request_id))
        object.__setattr__(self, "task_id", _require_id("task_id", self.task_id))
        object.__setattr__(self, "logical_session_id", _require_id("logical_session_id", self.logical_session_id))
        status = str(self.status).lower().strip()
        if status not in _RESULT_STATUSES:
            raise ValueError(f"unsupported result status: {self.status!r}")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "changed_files", _bounded_list("changed_files", self.changed_files, 500))
        object.__setattr__(self, "evidence_refs", _bounded_list("evidence_refs", self.evidence_refs, 100))
        for name in ("execution_status", "integration_status", "commit", "verification_tier", "event_idempotency_key", "provider", "usage_snapshot_ref", "error_code", "next_action", "rollback_ref"):
            object.__setattr__(self, name, _check_safe_text(name, getattr(self, name)))
        if self.status == "passed":
            if not self.verification_tier or not self.event_idempotency_key or not self.evidence_refs:
                raise ValueError("passed result requires verification, idempotency, and evidence references")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EdgeAgentResult":
        if payload.get("schema") != RESULT_SCHEMA:
            raise ValueError(f"unsupported result schema: {payload.get('schema')!r}")
        values = dict(payload)
        values.pop("schema", None)
        return cls(**values)
