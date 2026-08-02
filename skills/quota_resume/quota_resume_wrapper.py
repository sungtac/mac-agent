#!/usr/bin/env python3
"""Build safe, preview-only quota resume packets in the Edge Agent state root."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from skills.quota_resume import quota_resume


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"quota resume state is unreadable: {path}") from exc


def _context_packet(base_dir: Path, task: dict[str, Any]) -> dict[str, Any]:
    safe_task = quota_resume._redact(dict(task or {}))
    task_id = str(safe_task.get("task_id") or "UNKNOWN_TASK")
    safe_filename = "".join(character if character.isalnum() or character in "-_" else "_" for character in task_id).strip("._") or "UNKNOWN_TASK"
    packet_dir = quota_resume.state_dir(base_dir) / "context_packets"
    packet_dir.mkdir(parents=True, exist_ok=True)
    packet = {
        "schema": "edge_agent.context_packet.v1",
        "task_id": task_id,
        "created_at": datetime.now().isoformat(),
        "auto_execute": False,
        "requires_user_review": True,
        "task": safe_task,
    }
    json_path = packet_dir / f"{safe_filename}.json"
    markdown_path = packet_dir / f"{safe_filename}.md"
    quota_resume._atomic_write_json(json_path, packet)
    markdown = "\n".join([
        f"# Context Packet: {task_id}",
        "",
        "- auto_execute: false",
        "- requires_user_review: true",
        f"- objective: {safe_task.get('objective') or '확인 필요'}",
        "",
        "이 문서는 재개 검토용이며 자동 실행을 승인하지 않습니다.",
    ]) + "\n"
    quota_resume._atomic_write_text(markdown_path, markdown)
    return {"json_path": str(json_path), "markdown_path": str(markdown_path)}


def _preview_from_item(base_dir: Path, item: dict[str, Any], active_task: dict[str, Any]) -> dict[str, Any]:
    task = dict(active_task or {})
    task_id = str(item.get("task_id") or task.get("task_id") or "UNKNOWN_TASK")
    task.setdefault("task_id", task_id)
    return {
        "preview_id": f"PREVIEW_{task_id}_{int(datetime.now().timestamp())}",
        "task_id": task_id,
        "event_id": item.get("event_id"),
        "status": "preview_ready",
        "created_at": datetime.now().isoformat(),
        "auto_execute": False,
        "requires_user_review": True,
        "context_packet": _context_packet(base_dir, task),
        "summary": task.get("objective") or "Quota resume candidate",
    }


def build_ready_resume_previews(base_dir: str | Path) -> dict[str, Any]:
    base = Path(base_dir)
    quota_resume.ensure_state_files(base)
    ready = quota_resume.ready_queue_items(base)
    active = quota_resume.load_active_task(base)
    previews_path = quota_resume.state_dir(base) / "resume_previews.json"
    built = []
    for item in ready:
        key = (item.get("task_id"), item.get("event_id"))
        preview = _preview_from_item(base, item, active)
        def add_preview(store: Any) -> dict[str, Any] | None:
            if not isinstance(store, dict):
                raise ValueError(f"quota resume preview state must be an object: {previews_path}")
            previews = store.setdefault("previews", [])
            if not isinstance(previews, list):
                raise ValueError(f"quota resume preview list is invalid: {previews_path}")
            if any(isinstance(existing, dict) and (existing.get("task_id"), existing.get("event_id")) == key for existing in previews):
                return None
            previews.append(preview)
            store["updated_at"] = datetime.now().isoformat()
            return preview

        added = quota_resume._locked_json_update(previews_path, {"previews": []}, add_preview)
        if added is not None:
            built.append(added)
        quota_resume.update_resume_queue_item_status(base, str(item.get("task_id")), "preview_ready", event_id=item.get("event_id"))
    return {
        "status": "success",
        "preview_count": len(built),
        "context_packet_count": len(built),
        "previews": built,
        "context_packets": [item["context_packet"] for item in built],
        "auto_execute": False,
        "requires_user_review": True,
    }


def list_resume_previews(base_dir: str | Path) -> list[dict[str, Any]]:
    data = _load_json(quota_resume.state_dir(base_dir) / "resume_previews.json", {"previews": []})
    return [quota_resume._redact(item) for item in data.get("previews", []) if isinstance(item, dict)] if isinstance(data, dict) else []


def render_resume_list(base_dir: str | Path) -> str:
    previews = list_resume_previews(base_dir)
    if not previews:
        return "재개 가능한 작업 preview가 없습니다."
    lines = ["재개 가능한 작업 목록"]
    lines.extend(f"- 작업 재개 {item.get('task_id')}: {item.get('summary', 'resume preview')} / auto_execute: false" for item in previews[-10:])
    return "\n".join(lines)


def render_resume_preview(base_dir: str | Path, task_id: str) -> str:
    for item in reversed(list_resume_previews(base_dir)):
        if item.get("task_id") == task_id:
            packet = item.get("context_packet") or {}
            markdown_path = packet.get("markdown_path") if isinstance(packet, dict) else None
            content = ""
            if markdown_path:
                candidate = Path(markdown_path).expanduser().resolve()
                packet_root = (quota_resume.state_dir(base_dir) / "context_packets").resolve()
                try:
                    candidate.relative_to(packet_root)
                except ValueError:
                    candidate = None
                if candidate is not None and candidate.is_file():
                    content = candidate.read_text(encoding="utf-8")
            return "\n".join([
                f"Context Packet: {task_id}",
                "auto_execute: false",
                "requires_user_review: true",
                "",
                content[:2500] if content else str(packet),
            ]).strip()
    return f"작업 재개 preview를 찾지 못했습니다: {task_id}"
