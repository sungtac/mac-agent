#!/usr/bin/env python3
"""Plan safe next actions for high-priority active Hermes items.

This is deliberately read-only.  It explains why an item is still active and
which evidence path could move it to mitigated/live_verified later.  It never
sends Telegram messages, restarts Gateway, deletes files, or rewrites the Hermes
ledger.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.hermes_runtime.hermes_lifecycle_common import ACTIVE_STAGES, high_priority_records, raw_status, record_text
from skills.hermes_runtime.hermes_lifecycle_gate import DEFAULT_LOG


@dataclass
class ActivePlanItem:
    index: int
    title: str
    status: str
    priority: int
    reason_active: str
    safe_next_actions: list[str] = field(default_factory=list)
    blocked_actions: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)


@dataclass
class ActiveResolutionReport:
    ok: bool
    active_high_priority: int
    safely_actionable: int
    blocked: int
    items: list[ActivePlanItem]
    limits: list[str]


def _classify(record: dict[str, Any]) -> tuple[str, list[str], list[str], list[str]]:
    text = record_text(record, "title", "validation", "harnessChanges", "notes", lowercase=True)
    safe: list[str] = []
    blocked: list[str] = []
    required: list[str] = []
    reason = "high-priority item is still active"

    if "gateway restart" in text or "countdown" in text:
        reason = "Gateway restart/countdown behavior needs host/runtime proof and must not be exercised implicitly."
        safe.extend([
            "inspect existing gateway restart processed/status artifacts",
            "run static restart reporting contract tests",
        ])
        blocked.append("do not restart Gateway just to collect evidence")
        required.extend(["processed restart artifact with final phase", "runtime/status proof without interrupted reporting"])
    elif "media" in text or "hwpx" in text or "zip" in text or "document" in text:
        reason = "Telegram/HWPX transport needs messageId or delivery-layer proof; MEDIA resend is explicitly unsafe."
        safe.extend([
            "run attachment/final-requirement delivery audits on prepared files",
            "inspect existing request_telegram_document processed artifacts",
        ])
        blocked.append("do not send files/messages just to collect evidence")
        required.extend(["Telegram document messageId", "extension-preserving transport proof"])
    elif "telegram" in text and ("progress" in text or "작업 중" in text):
        reason = "Native Telegram progress needs host/runtime artifact; sandbox-only proof is insufficient."
        safe.extend([
            "queue/read native progress host probe artifacts",
            "run check_telegram_native_progress static/runtime scanner",
        ])
        blocked.append("do not send user-visible progress test messages just to collect evidence")
        required.extend(["host processed native-progress probe", "runtime marker proof", "messageId if user-visible behavior is claimed"])
    elif "sandbox" in text:
        reason = "Sandbox cannot prove host runtime behavior."
        safe.append("use host-helper request artifact instead of sandbox probe")
        required.append("host processed probe artifact")
    else:
        safe.append("add mitigation gate and explicit validation evidence")
        required.append("mitigation evidence")

    actionable = bool(safe) and not any("messageId" in item for item in required)
    return reason, safe, blocked, required


def evaluate(path: str | Path = DEFAULT_LOG, *, limit: int = 100) -> ActiveResolutionReport:
    items: list[ActivePlanItem] = []
    for idx, record, priority, stage in high_priority_records(path):
        title_lower = str(record.get("title") or "").lower()
        if "답변 형식" in title_lower or "간격 선호" in title_lower or "ux" in title_lower:
            continue

        status = raw_status(record, stage)
        if stage not in ACTIVE_STAGES and status.lower().replace("-", "_") not in ACTIVE_STAGES:
            continue
        reason, safe, blocked, required = _classify(record)
        items.append(ActivePlanItem(
            index=idx,
            title=str(record.get("title") or "untitled"),
            status=status,
            priority=priority,
            reason_active=reason,
            safe_next_actions=safe,
            blocked_actions=blocked,
            required_evidence=required,
        ))
    items.sort(key=lambda item: (-item.priority, item.title))
    safely_actionable = sum(1 for item in items if item.safe_next_actions)
    return ActiveResolutionReport(
        ok=len(items) == 0,
        active_high_priority=len(items),
        safely_actionable=safely_actionable,
        blocked=len(items),
        items=items[:limit],
        limits=[
            "Read-only planner; it does not mutate Hermes records.",
            "Actions requiring Telegram sends or Gateway restarts remain blocked unless explicitly requested.",
            "A mitigation plan is not live evidence and must not be reported as retired.",
        ],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan safe resolution paths for active high-priority Hermes items")
    parser.add_argument("--path", default=str(DEFAULT_LOG))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = evaluate(args.path, limit=args.limit)
    payload = asdict(report)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Hermes active resolution: active={report.active_high_priority} safe_paths={report.safely_actionable}")
        for item in report.items[:20]:
            print(f"- {item.title}: {item.reason_active}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
