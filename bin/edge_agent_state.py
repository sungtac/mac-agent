#!/usr/bin/env python3
"""Small, dependency-free runtime handoff state for terminal/Telegram bridges."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_DIR = Path(os.environ.get("EDGE_AGENT_STATE_DIR", str(Path.home() / ".edge-agent" / "state"))).expanduser()


def _task_id(role: str, chat_id: object, text: str) -> str:
    digest = hashlib.sha256(f"{role}|{chat_id}|{text}".encode("utf-8")).hexdigest()[:16]
    return f"{role}-{digest}"


def write_task_state(*, role: str, chat_id: object, text: str, status: str, **extra: Any) -> str:
    """Atomically write one task record and a latest pointer; never stores secrets."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    task_id = _task_id(role, chat_id, text)
    record = {
        "task_id": task_id,
        "role": role,
        "chat_id": str(chat_id),
        "status": status,
        "request_preview": text[:1000],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    target = STATE_DIR / f"{task_id}.json"
    for path in (target, STATE_DIR / "latest.json"):
        fd, temp_name = tempfile.mkstemp(prefix=".state-", dir=STATE_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    return task_id
