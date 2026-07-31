#!/usr/bin/env python3
"""Durable, local approval state for the Telegram research/plan gate."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PLAN_DIR = Path(os.environ.get("EDGE_AGENT_PLAN_DIR", str(Path.home() / ".edge-agent" / "plans"))).expanduser()
_APPROVAL_RE = re.compile(r"^\s*(?:실행\s*)?(?:승인|계속\s*진행|진행해)\s*[.!。]?\s*$", re.IGNORECASE)


def is_approval(text: str) -> bool:
    return bool(_APPROVAL_RE.fullmatch(text or ""))


def _path(chat_id: object) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", str(chat_id))
    return PLAN_DIR / f"chat-{safe}.json"


def save_pending(*, chat_id: object, task_id: str, request: str, plan: str, workspace: str) -> None:
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "chat_id": str(chat_id),
        "task_id": task_id,
        "request": request,
        "plan": plan,
        "workspace": workspace,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "awaiting_approval",
    }
    target = _path(chat_id)
    fd, temp_name = tempfile.mkstemp(prefix=".plan-", dir=PLAN_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def load_pending(chat_id: object) -> dict[str, Any] | None:
    target = _path(chat_id)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return data if data.get("status") == "awaiting_approval" else None


def clear_pending(chat_id: object) -> None:
    try:
        _path(chat_id).unlink()
    except FileNotFoundError:
        pass
