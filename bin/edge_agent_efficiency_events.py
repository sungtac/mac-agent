#!/usr/bin/env python3
"""Idempotent, non-sensitive efficiency evidence for A/B pilot comparisons."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA = "edge_agent.efficiency_event.v1"
_SENSITIVE = ("token=", "api_key", "authorization:", "bearer ", "password=", "cookie:", "secret=")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(value: str) -> str:
    text = str(value or "")
    if any(marker in text.casefold() for marker in _SENSITIVE):
        raise ValueError("efficiency evidence contains sensitive material")
    return text


@dataclass(frozen=True)
class EfficiencyEvent:
    task_id: str
    step_id: str
    event_idempotency_key: str
    provider: str
    profile: str
    status: str
    context_chars: int = 0
    prompt_chars: int = 0
    output_chars: int = 0
    tool_turns: int = 0
    changed_files: int = 0
    duration_ms: int = 0
    verification_tier: str = ""
    recorded_at: str = field(default_factory=_now)
    schema: str = field(default=SCHEMA, init=False)

    def __post_init__(self) -> None:
        for name in ("task_id", "step_id", "event_idempotency_key", "provider", "profile", "status", "verification_tier"):
            _safe(getattr(self, name))
        if not self.task_id or not self.step_id or not self.event_idempotency_key:
            raise ValueError("task_id, step_id, and event_idempotency_key are required")
        if min(self.context_chars, self.prompt_chars, self.output_chars, self.tool_turns, self.changed_files, self.duration_ms) < 0:
            raise ValueError("efficiency counters must not be negative")

    def to_dict(self) -> dict:
        return asdict(self)


class EfficiencyStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or Path.home() / ".edge-agent" / "efficiency").expanduser().resolve()
        self.path = self.root / "events.jsonl"
        self.lock_path = self.root / "events.lock"

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def append(self, event: EfficiencyEvent) -> str:
        with self._lock():
            existing = []
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    existing.append(json.loads(line))
            for item in existing:
                if item.get("event_idempotency_key") == event.event_idempotency_key:
                    if item == event.to_dict():
                        return "duplicate"
                    raise ValueError("efficiency event idempotency conflict")
            descriptor, temporary = tempfile.mkstemp(prefix=".events.", dir=self.root)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    for item in existing:
                        stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
                    stream.write(json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
            return "recorded"
