"""Small provider-neutral trace context for durable agent events."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import uuid
from typing import Any, Mapping


SCHEMA = "edge_agent.trace_context.v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _id(name: str, value: object) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID.fullmatch(text):
        raise ValueError(f"{name} must be a safe non-empty identifier")
    return text


def _digest(*parts: object, length: int) -> str:
    material = "\x00".join(str(part) for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:length]


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: str = ""
    sampled: bool = True
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace_id", _id("trace_id", self.trace_id))
        object.__setattr__(self, "span_id", _id("span_id", self.span_id))
        if self.parent_span_id:
            object.__setattr__(self, "parent_span_id", _id("parent_span_id", self.parent_span_id))
        if not isinstance(self.sampled, bool):
            raise ValueError("sampled must be boolean")
        if self.schema != SCHEMA:
            raise ValueError("unsupported trace schema")

    @classmethod
    def for_message(
        cls,
        *,
        session_id: str,
        task_id: str,
        source_event_id: str,
        from_role: str,
        purpose: str,
        round_number: int,
        trace_id: str = "",
        parent_span_id: str = "",
    ) -> "TraceContext":
        return cls(
            trace_id=trace_id or f"trace-{_digest(session_id, task_id, length=32)}",
            span_id=f"span-{_digest(source_event_id, from_role, purpose, round_number, uuid.uuid4().hex, length=16)}",
            parent_span_id=parent_span_id,
        )

    def child(self, *, source_event_id: str, from_role: str, purpose: str, round_number: int) -> "TraceContext":
        return TraceContext(
            trace_id=self.trace_id,
            span_id=f"span-{_digest(source_event_id, from_role, purpose, round_number, uuid.uuid4().hex, length=16)}",
            parent_span_id=self.span_id,
            sampled=self.sampled,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "sampled": self.sampled,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TraceContext":
        if payload.get("schema") != SCHEMA:
            raise ValueError("unsupported trace schema")
        values = dict(payload)
        values.pop("schema", None)
        return cls(**values)


def event_trace(session_id: str, task_id: str = "", *, parent_span_id: str = "") -> TraceContext:
    """Create a durable event span while retaining one session trace id."""

    session = _id("session_id", session_id)
    return TraceContext(
        trace_id=f"trace-{_digest(session, length=32)}",
        span_id=f"span-{uuid.uuid4().hex[:16]}",
        parent_span_id=parent_span_id,
    )


__all__ = ["SCHEMA", "TraceContext", "event_trace"]
