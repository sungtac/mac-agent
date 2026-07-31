#!/usr/bin/env python3
"""Preview and approved apply helpers for LLM fallback switching."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from skills.quota_resume import quota_resume


def build_fallback_switch_preview(event: dict | None = None) -> dict:
    event = event or {}
    return {
        "preview_id": f"FALLBACK_{int(datetime.now().timestamp())}",
        "task_id": event.get("task_id") or "UNKNOWN_TASK",
        "event_id": event.get("event_id"),
        "type": event.get("type") or "rate_limit",
        "source_llm": "core",
        "target_llm": "worker",
        "ok": True,
        "status": "pending_approval",
        "created_at": datetime.now().isoformat(),
        "auto_execute": False,
        "requires_user_review": True,
        "safe_policy": "preview_only_until_user_approval",
    }


def _store_path(base_dir: str | Path) -> Path:
    quota_resume.ensure_state_files(base_dir)
    return quota_resume.state_dir(base_dir) / "fallback_switch_previews.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"previews": []}
    except Exception:
        return {"previews": []}


def save_fallback_switch_preview(base_dir: str | Path, preview: dict) -> dict:
    path = _store_path(base_dir)
    data = _load(path)
    data.setdefault("previews", [])
    data["previews"].append(dict(preview, auto_execute=False, requires_user_review=True))
    data["updated_at"] = datetime.now().isoformat()
    quota_resume._atomic_write_json(path, data)
    return {"status": "success", "preview_id": preview.get("preview_id")}


def latest_pending_preview(base_dir: str | Path) -> dict | None:
    data = _load(_store_path(base_dir))
    for preview in reversed(data.get("previews", [])):
        if isinstance(preview, dict) and preview.get("status") in ("pending_approval", "preview_ready"):
            return preview
    return None


def apply_latest_fallback_switch(base_dir: str | Path, chat_id: str | int = "") -> dict:
    # A provider/account switch is an external authority change.  The portable
    # skill only prepares the preview; a separately approved connector must
    # perform the actual switch and report evidence back to this store.
    del base_dir, chat_id
    return {
        "ok": False,
        "status": "blocked",
        "error": "provider_switch_connector_required",
        "auto_execute": False,
        "requires_user_review": True,
    }
