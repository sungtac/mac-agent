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
    return quota_resume._redact({
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
    })


def _store_path(base_dir: str | Path) -> Path:
    quota_resume.ensure_state_files(base_dir)
    return quota_resume.state_dir(base_dir) / "fallback_switch_previews.json"


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"previews": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"fallback preview state is unreadable: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"fallback preview state must be an object: {path}")
    return data


def save_fallback_switch_preview(base_dir: str | Path, preview: dict) -> dict:
    path = _store_path(base_dir)
    safe_preview = quota_resume._redact(dict(preview, auto_execute=False, requires_user_review=True))

    def update(data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError(f"fallback preview state must be an object: {path}")
        previews = data.setdefault("previews", [])
        if not isinstance(previews, list):
            raise ValueError(f"fallback preview list is invalid: {path}")
        if any(isinstance(item, dict) and item.get("preview_id") == safe_preview.get("preview_id") for item in previews):
            return {"status": "success", "preview_id": safe_preview.get("preview_id"), "duplicate": True}
        previews.append(safe_preview)
        data["updated_at"] = datetime.now().isoformat()
        return {"status": "success", "preview_id": safe_preview.get("preview_id"), "duplicate": False}

    result = quota_resume._locked_json_update(path, {"previews": []}, update)
    return result


def latest_pending_preview(base_dir: str | Path) -> dict | None:
    data = _load(_store_path(base_dir))
    for preview in reversed(data.get("previews", [])):
        if isinstance(preview, dict) and preview.get("status") in ("pending_approval", "preview_ready"):
            return quota_resume._redact(preview)
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
