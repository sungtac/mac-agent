#!/usr/bin/env python3
"""Preview-only quota resume state helpers.

This module intentionally does not resume work or switch accounts by itself.  It
records quota events and builds reviewable state using locked, atomic JSON
writes so recovery flows can be inspected safely.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

STATE_FILES: dict[str, Any] = {
    "active_task.json": {},
    "quota_events.json": {"events": []},
    "resume_queue.json": {"items": []},
    "resume_previews.json": {"previews": []},
    "fallback_switch_previews.json": {"previews": []},
}
SENSITIVE_PATTERNS = [
    re.compile(r"(?i)sk-[a-z0-9_-]+"),
    re.compile(r"(?i)(token|secret|password|api[_ -]?key)(\s*[:=]\s*)[^\s,'\"]+"),
]


def _now_iso() -> str:
    return datetime.now().isoformat()


def state_dir(base_dir: str | Path) -> Path:
    return Path(base_dir) / "state"


def _default_for(name: str) -> Any:
    value = STATE_FILES[name]
    return json.loads(json.dumps(value))


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    text = str(value) if not isinstance(value, (int, float, bool, type(None))) else value
    if not isinstance(text, str):
        return text
    safe = text
    for pattern in SENSITIVE_PATTERNS:
        if pattern.pattern.startswith("(?i)(token"):
            safe = pattern.sub(r"\1\2[REDACTED]", safe)
        else:
            safe = pattern.sub("[REDACTED]", safe)
    return safe


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if data is not None else json.loads(json.dumps(default))
    except Exception:
        return json.loads(json.dumps(default))


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _locked_json_update(path: Path, default: Any, updater: Callable[[Any], Any]) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = _load_json(path, default)
        result = updater(data)
        _atomic_write_json(path, data)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return result


def ensure_state_files(base_dir: str | Path) -> dict:
    sdir = state_dir(base_dir)
    sdir.mkdir(parents=True, exist_ok=True)
    created = []
    for name in STATE_FILES:
        path = sdir / name
        if not path.exists():
            _atomic_write_json(path, _default_for(name))
            created.append(str(path))
    packets = sdir / "context_packets"
    packets.mkdir(parents=True, exist_ok=True)
    return {"status": "success", "state_dir": str(sdir), "created": created}


def save_active_task(base_dir: str | Path, task: dict) -> dict:
    ensure_state_files(base_dir)
    payload = _redact(dict(task or {}))
    payload.setdefault("task_id", f"TASK_{int(datetime.now().timestamp())}")
    payload["updated_at"] = _now_iso()
    _atomic_write_json(state_dir(base_dir) / "active_task.json", payload)
    return {"status": "success", "task_id": payload.get("task_id")}


def load_active_task(base_dir: str | Path) -> dict:
    ensure_state_files(base_dir)
    data = _load_json(state_dir(base_dir) / "active_task.json", {})
    return data if isinstance(data, dict) else {}


def record_quota_event(base_dir: str | Path, event: dict) -> dict:
    ensure_state_files(base_dir)
    event_id = str((event or {}).get("event_id") or f"QEVT_{int(datetime.now().timestamp())}")
    payload = _redact(dict(event or {}))
    payload.update({"event_id": event_id, "recorded_at": _now_iso()})

    def update(data: dict) -> dict:
        data.setdefault("events", [])
        data["events"].append(payload)
        return payload

    written = _locked_json_update(state_dir(base_dir) / "quota_events.json", {"events": []}, update)
    return {"status": "success", "event_id": event_id, "event": written}


def add_resume_queue_item(base_dir: str | Path, item: dict) -> dict:
    ensure_state_files(base_dir)
    payload = _redact(dict(item or {}))
    payload.setdefault("task_id", load_active_task(base_dir).get("task_id") or "UNKNOWN_TASK")
    payload.setdefault("event_id", f"QEVT_{int(datetime.now().timestamp())}")
    payload.setdefault("type", "rate_limit")
    payload.setdefault("resume_after", (datetime.now() + timedelta(minutes=10)).isoformat())
    payload.setdefault("status", "waiting")
    payload["requires_user_review"] = True
    payload["auto_execute"] = False
    payload["updated_at"] = _now_iso()

    def update(data: dict) -> dict:
        data.setdefault("items", [])
        data["items"].append(payload)
        return payload

    written = _locked_json_update(state_dir(base_dir) / "resume_queue.json", {"items": []}, update)
    return {"status": "success", "task_id": written.get("task_id"), "event_id": written.get("event_id")}


def update_resume_queue_item_status(base_dir: str | Path, task_id: str, status: str, event_id: str | None = None) -> dict:
    ensure_state_files(base_dir)

    def update(data: dict) -> dict:
        updated = 0
        for item in data.get("items", []):
            if item.get("task_id") == task_id and (event_id is None or item.get("event_id") == event_id):
                item["status"] = status
                item["updated_at"] = _now_iso()
                updated += 1
        return {"status": "success", "updated": updated}

    return _locked_json_update(state_dir(base_dir) / "resume_queue.json", {"items": []}, update)


def ready_queue_items(base_dir: str | Path, now: datetime | None = None) -> list[dict]:
    ensure_state_files(base_dir)
    current = now or datetime.now()
    data = _load_json(state_dir(base_dir) / "resume_queue.json", {"items": []})
    ready = []
    for item in data.get("items", []):
        if item.get("status") not in ("waiting", "preview_ready"):
            continue
        try:
            resume_after = datetime.fromisoformat(str(item.get("resume_after")))
        except Exception:
            resume_after = current
        if resume_after <= current:
            ready.append(item)
    return ready


def maybe_resume_autoloop_after_debounce(*, now: datetime | None = None, dry_run: bool = True, state_path: str | Path, context_path: str | Path | None = None) -> dict:
    from progress_registry import AutoloopRegistry  # imported lazily for jarvis path tests

    registry = AutoloopRegistry(state_path=Path(state_path), context_path=Path(context_path) if context_path else None)
    registry.load()
    if registry.state != "PAUSED":
        return {"should_resume": False, "action": "NOOP", "state": registry.state}
    current = now or datetime.now()
    try:
        last_updated = datetime.fromisoformat(registry.last_updated)
    except Exception:
        last_updated = current
    if current - last_updated < timedelta(minutes=10):
        return {"should_resume": False, "action": "WAIT", "state": registry.state}
    if dry_run:
        return {"should_resume": True, "action": "WOULD_TRANSITION", "auto_execute": False, "requires_user_review": True}
    return {"should_resume": True, "action": "READY_FOR_USER_REVIEW", "new_state": registry.state, "auto_execute": False, "requires_user_review": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_dir", nargs="?", default=".")
    parser.add_argument("--record-event", default="")
    args = parser.parse_args()
    if args.record_event:
        event = json.loads(args.record_event)
        print(json.dumps(record_quota_event(args.base_dir, event), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(ensure_state_files(args.base_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
