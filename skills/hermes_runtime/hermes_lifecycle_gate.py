#!/usr/bin/env python3
"""Hermes lifecycle gate: keep mitigated, live-verified, and retired distinct.

This gate does not retire issues by itself.  It checks whether high-priority
Hermes records carry enough stage/evidence metadata to justify their current
state, and it reports exactly what is still needed before retirement.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.hermes_runtime.hermes_lifecycle_common import high_priority_records, lifecycle_stage, redact_text

DEFAULT_LOG = Path(
    __import__("os").environ.get("EDGE_AGENT_HERMES_LOG", "~/.edge-agent/state/hermes-feedback.jsonl")
).expanduser().resolve()
HIGH_PRIORITY = 90
ACTIVE_STATUSES = {"proposed", "blocked"}
MITIGATED_STATUSES = {"implemented", "validated", "mitigated"}
RETIRED_STATUSES = {"retired", "live_verified", "live-verified"}
STATIC_VERIFIED_STATUSES = {"static_verified", "static-verified"}


@dataclass
class LifecycleIssue:
    level: str
    rule: str
    title: str
    message: str
    missing: list[str] = field(default_factory=list)


@dataclass
class LifecycleReport:
    ok: bool
    score: int
    max_score: int
    high_priority_total: int
    active_high_priority: int
    mitigated_high_priority: int
    live_verified_high_priority: int
    retired_high_priority: int
    static_verified_high_priority: int
    issues: list[LifecycleIssue]


def _text(record: dict[str, Any], *keys: str) -> str:
    values: list[str] = []
    for key in keys:
        value = record.get(key)
        if isinstance(value, list):
            values.extend(str(v) for v in value)
        elif value is not None:
            values.append(str(value))
    return "\n".join(values)


def mitigation_evidence(record: dict[str, Any]) -> bool:
    text = _text(record, "validation", "harnessChanges", "harness", "required_gate", "requiredGate", "evidence_path", "evidencePath")
    return _adequate_evidence(text, minimum=8)


def live_evidence(record: dict[str, Any]) -> bool:
    text = _text(record, "liveEvidence", "live_evidence", "messageId", "message_id", "runtimeEvidence", "runtime_evidence")
    return _adequate_evidence(text, minimum=20)


def retirement_evidence(record: dict[str, Any]) -> bool:
    text = _text(record, "retirementEvidence", "retirement_evidence", "retiredAt", "retired_at")
    return _adequate_evidence(text, minimum=8)


def recurrence_free_window(record: dict[str, Any]) -> bool:
    text = _text(record, "recurrenceFreeWindow", "recurrence_free_window", "retirementWindow", "retirement_window")
    if not _adequate_evidence(text, minimum=3):
        return False
    return bool(re.search(r"\d+\s*(?:minutes?|hours?|days?|weeks?|months?|분|시간|일|주|개월)", text, re.IGNORECASE))


def _adequate_evidence(text: str, *, minimum: int) -> bool:
    normalized = " ".join(text.split()).casefold()
    if len(normalized) < minimum:
        return False
    return normalized not in {"x", "ok", "done", "verified", "true", "test"}


def static_evidence(record: dict[str, Any]) -> bool:
    text = _text(record, "staticEvidence", "static_evidence", "noLiveBoundary", "no_live_boundary")
    return bool(text.strip())


def evaluate(path: str | Path = DEFAULT_LOG) -> LifecycleReport:
    rows = high_priority_records(path)
    issues: list[LifecycleIssue] = []
    active = mitigated = live_verified = retired = static_verified = 0

    for _, record, _, stage in rows:
        title = redact_text(str(record.get("title") or "untitled"))
        if stage in ACTIVE_STATUSES:
            active += 1
            missing = []
            if not mitigation_evidence(record):
                missing.append("mitigation evidence")
            issues.append(LifecycleIssue("warning", "high.active", title, "high-priority item is still active; do not describe as retired", missing))
            continue
        if stage == "mitigated":
            mitigated += 1
            missing = []
            if not mitigation_evidence(record):
                missing.append("mitigation evidence")
            if not live_evidence(record):
                missing.append("live evidence/messageId/runtime proof before live_verified")
            if missing:
                issues.append(LifecycleIssue("warning", "mitigated.not-live-verified", title, "mitigated item still lacks retirement-grade evidence", missing))
            continue
        if stage == "live_verified":
            live_verified += 1
            missing = []
            if not mitigation_evidence(record):
                missing.append("mitigation evidence")
            if not live_evidence(record):
                missing.append("live evidence")
            if missing:
                issues.append(LifecycleIssue("error", "live-verified.evidence-missing", title, "live_verified requires live evidence", missing))
            continue
        if stage == "retired":
            retired += 1
            missing = []
            if not mitigation_evidence(record):
                missing.append("mitigation evidence")
            if not live_evidence(record):
                missing.append("live evidence")
            if not retirement_evidence(record):
                missing.append("retirement evidence/retiredAt")
            if not recurrence_free_window(record):
                missing.append("recurrence-free window")
            if missing:
                issues.append(LifecycleIssue("error", "retired.evidence-missing", title, "retired items require mitigation, live, and retirement evidence", missing))
            continue
        if stage == "static_verified":
            static_verified += 1
            missing = []
            if not mitigation_evidence(record):
                missing.append("mitigation evidence")
            if not static_evidence(record):
                missing.append("static evidence/no-live boundary")
            if missing:
                issues.append(LifecycleIssue("error", "static-verified.evidence-missing", title, "static_verified items require static evidence and no-live boundary", missing))
            continue
        active += 1
        issues.append(LifecycleIssue("warning", "stage.unknown", title, f"unknown lifecycle stage: {stage}", ["explicit lifecycleStage"]))

    score = 100
    score -= 20 * sum(1 for issue in issues if issue.level == "error")
    score -= min(30, 3 * sum(1 for issue in issues if issue.level == "warning"))
    score = max(0, score)
    ok = not any(issue.level == "error" for issue in issues)
    return LifecycleReport(
        ok=ok,
        score=score,
        max_score=100,
        high_priority_total=len(rows),
        active_high_priority=active,
        mitigated_high_priority=mitigated,
        live_verified_high_priority=live_verified,
        retired_high_priority=retired,
        static_verified_high_priority=static_verified,
        issues=issues,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Hermes high-priority lifecycle evidence")
    parser.add_argument("--path", default=str(DEFAULT_LOG))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = evaluate(args.path)
    payload = asdict(report)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Hermes lifecycle: {'PASS' if report.ok else 'FAIL'} score={report.score}/{report.max_score}")
        for issue in report.issues[:20]:
            print(f"- {issue.level.upper()} {issue.rule}: {issue.title} missing={', '.join(issue.missing) if issue.missing else '-'}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
