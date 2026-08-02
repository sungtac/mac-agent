"""Hermes feedback backlog manager.

Turns append-only hermes-feedback records into a lightweight backlog with stable
status buckets and priority scoring.  This is read-only by default.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import json
import os
import re

_SENSITIVE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]+\b"),
    re.compile(r"\b(?:ghp|github_pat)[_-][A-Za-z0-9_-]+\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9_-]+\b"),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE),
    re.compile(r"\b(?:token|secret|password|api[_ -]?key|authorization)\s*[:=]\s*[^\s,'\"]+", re.IGNORECASE),
)
_SENSITIVE_KEYS = {
    "token", "secret", "password", "api_key", "authorization", "cookie",
    "private_key", "client_secret", "access_token", "refresh_token", "credential",
}


def redact_text(value: str) -> str:
    safe = value
    for pattern in _SENSITIVE_PATTERNS:
        safe = pattern.sub("[REDACTED]", safe)
    safe = re.sub(r"https?://[^\s'\"<>/@]+:[^\s'\"<>/@]+@", "https://[REDACTED]@", safe, flags=re.IGNORECASE)
    return re.sub(
        r"(?i)(['\"]?(?:token|secret|password|api[_ -]?key|authorization|cookie|private[_ -]?key|credential)['\"]?\s*[:=]\s*['\"]?)[^\s,}\"']+",
        r"\1[REDACTED]",
        safe,
    )

STATUS_ORDER = ("proposed", "implemented", "validated", "mitigated", "live_verified", "retired", "blocked", "rejected")
VALID_STATUSES = set(STATUS_ORDER)
RISK_WEIGHTS = {"token": 30, "auth": 25, "restart": 20, "gateway": 15, "telegram": 15, "rollback": 20, "test": 10}


@dataclass(frozen=True)
class BacklogItem:
    title: str
    status: str
    priority: int
    validation: str
    risk: str
    files_changed: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_status(record: dict) -> str:
    status = str(record.get("status") or "proposed").lower().replace("-", "_")
    if status not in VALID_STATUSES:
        return "proposed"
    if status == "implemented" and record.get("validation"):
        return "validated"
    return status


def score_priority(record: dict) -> int:
    text = " ".join(str(record.get(k, "")) for k in ("title", "issue", "risk", "harnessChanges")) .lower()
    score = 50
    for key, weight in RISK_WEIGHTS.items():
        if key in text:
            score += weight
    if normalize_status(record) in {"blocked", "proposed"}:
        score += 20
    return min(score, 100)


def load_feedback_records(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    records = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
            else:
                records.append({"title": "MALFORMED_RECORD", "status": "blocked", "risk": "record is not an object"})
        except json.JSONDecodeError:
            records.append({"title": "UNPARSEABLE_RECORD", "status": "blocked", "risk": "invalid json"})
    return records


def build_backlog(path: str | Path) -> dict:
    records = load_feedback_records(path)
    items = []
    counts = {status: 0 for status in STATUS_ORDER}
    for record in records:
        status = normalize_status(record)
        counts[status] += 1
        raw_files = record.get("filesChanged") or record.get("files") or []
        if isinstance(raw_files, str):
            raw_files = [raw_files]
        if not isinstance(raw_files, (list, tuple)):
            raw_files = []
        files = tuple(redact_text(str(item)) for item in raw_files)
        items.append(BacklogItem(
            title=redact_text(str(record.get("title") or "untitled")),
            status=status,
            priority=score_priority(record),
            validation=redact_text(str(record.get("validation") or "")),
            risk=redact_text(str(record.get("risk") or "")),
            files_changed=files,
        ).to_dict())
    items.sort(key=lambda item: (-item["priority"], item["status"], item["title"]))
    return {"total": len(items), "counts": counts, "items": items}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Summarize Hermes feedback backlog")
    parser.add_argument(
        "path",
        nargs="?",
        default=os.environ.get("EDGE_AGENT_HERMES_LOG", "~/.edge-agent/state/hermes-feedback.jsonl"),
    )
    args = parser.parse_args()
    print(json.dumps(build_backlog(args.path), ensure_ascii=False, indent=2))
