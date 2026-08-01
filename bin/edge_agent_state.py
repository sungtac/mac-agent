#!/usr/bin/env python3
"""Small, dependency-free runtime handoff state for terminal/Telegram bridges.

``latest.json`` is a convenience pointer, not the session history.  Every
write is also appended to ``history.jsonl`` with an explicit UTC timestamp and
monotonic sequence.  Readers must select by those fields, never by directory
listing order or filesystem mtime.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


HISTORY_FILE = "history.jsonl"
LOCK_FILE = ".state.lock"

_SECRET_VALUE = r"(?i)(token|api[_ -]?key|authorization|bearer|password|cookie|secret|private[_ -]?key)\s*[:=]\s*[^\s,;]+"
_TELEGRAM_TOKEN = r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b"


def _task_id(role: str, chat_id: object, text: str) -> str:
    digest = hashlib.sha256(f"{role}|{chat_id}|{text}".encode("utf-8")).hexdigest()[:16]
    return f"{role}-{digest}"


def _state_dir() -> Path:
    """Resolve the root at call time so tests and adapters can inject it."""
    return Path(os.environ.get("EDGE_AGENT_STATE_DIR", str(Path.home() / ".edge-agent" / "state"))).expanduser()


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        import re
        value = re.sub(_TELEGRAM_TOKEN, "[redacted-token]", value)
        return re.sub(_SECRET_VALUE, lambda match: f"{match.group(1)}=[redacted]", value)
    if isinstance(value, Mapping):
        return {str(key): _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _now() -> tuple[str, float]:
    current = datetime.now(timezone.utc)
    return current.isoformat(), current.timestamp()


def _parse_epoch(value: Any) -> float:
    try:
        if value is not None and str(value).strip():
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _record_key(record: Mapping[str, Any]) -> tuple[float, int, str, str]:
    try:
        activity = float(record.get("updated_epoch") or _parse_epoch(record.get("updated_at")))
    except (TypeError, ValueError, OverflowError):
        activity = 0.0
    try:
        sequence = int(record.get("sequence") or 0)
    except (TypeError, ValueError, OverflowError):
        sequence = 0
    return (
        activity,
        sequence,
        str(record.get("updated_at") or ""),
        str(record.get("task_id") or ""),
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_history(root: Path) -> list[dict[str, Any]]:
    path = root / HISTORY_FILE
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("schema") == "edge_agent.task_state_event.v2":
            records.append(payload)
    return records


def _legacy_records(root: Path, known_task_ids: set[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        paths = sorted(root.glob("*.json"), key=lambda item: item.name)
    except OSError:
        return records
    for path in paths:
        if path.name == "latest.json":
            continue
        payload = _read_json(path)
        if not payload or not payload.get("task_id") or payload["task_id"] in known_task_ids:
            continue
        payload["source"] = "legacy_task_state"
        payload["legacy"] = True
        records.append(payload)
    return records


def _attach_delivery_tail(root: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Recover the exact final response tail from the Telegram outbox when available."""
    if record.get("response_tail") or not record.get("delivery_id"):
        return record
    delivery_id = str(record.get("delivery_id"))
    if not delivery_id or any(char in delivery_id for char in "/\\\x00"):
        return record
    payload = _read_json(root / "telegram-delivery" / f"{delivery_id}.json")
    chunks = payload.get("chunks") if payload else None
    if isinstance(chunks, list) and all(isinstance(chunk, str) for chunk in chunks):
        record["response_tail"] = _redact("".join(chunks)[-1000:])
        record["response_tail_source"] = "telegram_delivery"
    return record


def list_task_states(*, role: str = "", chat_id: str = "", workspace: str = "") -> list[dict[str, Any]]:
    """Return persisted task states ordered by explicit activity metadata."""
    root = _state_dir()
    history = _read_history(root)
    known = {str(item.get("task_id")) for item in history if item.get("task_id")}
    records = [_attach_delivery_tail(root, dict(item)) for item in history + _legacy_records(root, known)]
    filtered = []
    for record in records:
        if role and str(record.get("role")) != role:
            continue
        if chat_id and str(record.get("chat_id")) != str(chat_id):
            continue
        if workspace and str(record.get("workspace")) != str(workspace):
            continue
        filtered.append(record)
    return sorted(filtered, key=_record_key, reverse=True)


def latest_task_state(*, role: str = "", chat_id: str = "", workspace: str = "") -> dict[str, Any] | None:
    """Select one latest state using timestamp → sequence → stable ID tie-breaks."""
    records = list_task_states(role=role, chat_id=chat_id, workspace=workspace)
    if not records:
        return None
    selected = dict(records[0])
    selected["selection"] = {
        "method": "updated_at_then_sequence",
        "candidate_count": len(records),
        "updated_at": selected.get("updated_at", ""),
        "updated_epoch": selected.get("updated_epoch", _parse_epoch(selected.get("updated_at"))),
    }
    return selected


def _next_sequence(root: Path) -> int:
    records = _read_history(root)
    return max((int(item.get("sequence") or 0) for item in records), default=0) + 1


def write_task_state(*, role: str, chat_id: object, text: str, status: str, **extra: Any) -> str:
    """Write one state snapshot and append an immutable, ordered history event."""
    root = _state_dir()
    root.mkdir(parents=True, exist_ok=True)
    task_id = _task_id(role, chat_id, text)
    updated_at, updated_epoch = _now()
    record = _redact({
        "schema": "edge_agent.task_state_event.v2",
        "task_id": task_id,
        "role": role,
        "chat_id": str(chat_id),
        "status": status,
        "request_preview": str(text)[:1000],
        "request_tail": str(text)[-1000:],
        "updated_at": updated_at,
        "updated_epoch": updated_epoch,
        "sequence": 0,
        **extra,
    })
    lock_path = root / LOCK_FILE
    descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        record["sequence"] = _next_sequence(root)
        target = root / f"{task_id}.json"
        history = root / HISTORY_FILE
        with history.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(history, 0o600)
        for path in (target, root / "latest.json"):
            fd, temp_name = tempfile.mkstemp(prefix=".state-", dir=root)
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
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    return task_id


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Deterministic Edge Agent task-state lookup")
    parser.add_argument("command", choices=("latest", "list"))
    parser.add_argument("--role", default="")
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    records = list_task_states(role=args.role, chat_id=args.chat_id, workspace=args.workspace)
    if args.command == "latest":
        payload = latest_task_state(role=args.role, chat_id=args.chat_id, workspace=args.workspace)
        print(json.dumps(payload or {"status": "not_found", "selection": {"candidate_count": 0}}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"records": records[: max(1, min(args.limit, 100))], "count": len(records)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
