#!/usr/bin/env python3
"""Hermes quota-event hook that records preview-only recovery state."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from skills.quota_resume import fallback_switch_preview, quota_resume, quota_resume_wrapper


def _error_text(event: dict[str, Any]) -> str:
    return str(event.get("error") or event.get("raw_error") or event.get("message") or "")


def _is_quota_error(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("quota", "rate_limit", "rate limit", "insufficient_quota", "429", "한도", "쿼터"))


def record_hermes_quota_event(base_dir: str | Path, event: dict[str, Any]) -> dict:
    text = _error_text(event)
    if not _is_quota_error(text):
        return {"status": "ignored", "reason": "not_quota_error", "fallback_switch_preview": None}
    event_id = str(event.get("event_id") or f"HERMES_QUOTA_{int(datetime.now().timestamp())}")
    task_id = str(event.get("task_id") or "HERMES_QUOTA_RESUME")
    quota_event = dict(event)
    quota_event.update({"event_id": event_id, "task_id": task_id, "type": "rate_limit", "raw_error": text})
    rec_res = quota_resume.record_quota_event(base_dir, quota_event)
    queue_item = {
        "task_id": task_id,
        "event_id": event_id,
        "type": "rate_limit",
        "resume_after": (datetime.now() + timedelta(minutes=10)).isoformat(),
        "status": "waiting",
        "requires_user_review": True,
        "reason": text[:500],
    }
    q_res = quota_resume.add_resume_queue_item(base_dir, queue_item)
    preview = fallback_switch_preview.build_fallback_switch_preview(queue_item)
    fallback_switch_preview.save_fallback_switch_preview(base_dir, preview)
    return {
        "status": "success",
        "quota_event": rec_res,
        "resume_queue": q_res,
        "resume_preview": quota_resume_wrapper.build_ready_resume_previews(base_dir),
        "fallback_switch_preview": preview,
        "auto_execute": False,
        "requires_user_review": True,
    }
