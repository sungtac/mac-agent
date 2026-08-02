"""Hermes feedback backlog manager.

Turns append-only hermes-feedback records into a lightweight backlog with stable
status buckets and priority scoring.  This is read-only by default.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import json
import os

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
            records.append(json.loads(line))
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
        files = tuple(record.get("filesChanged") or record.get("files") or [])
        items.append(BacklogItem(
            title=str(record.get("title") or "untitled"),
            status=status,
            priority=score_priority(record),
            validation=str(record.get("validation") or ""),
            risk=str(record.get("risk") or ""),
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
