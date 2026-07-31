#!/usr/bin/env python3
"""Plan and apply Hermes live-evidence lifecycle updates.

This is the safe bridge from "mitigated" to "live_verified"/"retired": it
requires explicit live evidence text (for example messageId/runtime probe output)
and keeps a timestamped backup before rewriting the JSONL ledger.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.hermes_runtime.hermes_lifecycle_common import high_priority_records, priority_of
from skills.hermes_runtime.hermes_lifecycle_gate import DEFAULT_LOG, lifecycle_stage, live_evidence, mitigation_evidence, retirement_evidence


@dataclass
class Candidate:
    index: int
    title: str
    stage: str
    priority: int
    can_live_verify: bool
    can_retire: bool
    missing_for_live_verified: list[str] = field(default_factory=list)
    missing_for_retired: list[str] = field(default_factory=list)


@dataclass
class EvidenceResult:
    ok: bool
    action: str
    matched: int
    updated: int
    path: str
    backup: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def load_lines(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    if not path.exists():
        return [], []
    lines = path.read_text(encoding="utf-8").splitlines()
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        records.append(json.loads(line))
    return lines, records


def candidate_for(index: int, record: dict[str, Any], *, priority: int | None = None, stage: str | None = None) -> Candidate:
    stage = stage or lifecycle_stage(record)
    missing_live: list[str] = []
    if not mitigation_evidence(record):
        missing_live.append("mitigation evidence")
    if not live_evidence(record):
        missing_live.append("live evidence/messageId/runtime proof")
    missing_retired = list(missing_live)
    if not retirement_evidence(record):
        missing_retired.append("retirement evidence/retiredAt")
    return Candidate(
        index=index,
        title=str(record.get("title") or "untitled"),
        stage=stage,
        priority=int(priority if priority is not None else priority_of(record)),
        can_live_verify=stage == "mitigated" and not missing_live,
        can_retire=stage in {"mitigated", "live_verified"} and not missing_retired,
        missing_for_live_verified=missing_live,
        missing_for_retired=missing_retired,
    )


def plan(path: str | Path = DEFAULT_LOG) -> EvidenceResult:
    rows = high_priority_records(path)
    candidates = [candidate_for(i, r, priority=priority, stage=stage) for i, r, priority, stage in rows if stage in {"mitigated", "live_verified"}]
    # Show the most actionable first: missing only live evidence, then all others.
    candidates.sort(key=lambda c: (len(c.missing_for_live_verified), -c.priority, c.title))
    return EvidenceResult(True, "plan", len(candidates), 0, str(path), candidates=candidates[:50])


def match_records(records: list[dict[str, Any]], title: str, contains: bool) -> list[int]:
    needle = title.lower()
    hits = []
    for idx, record in enumerate(records):
        hay = str(record.get("title") or "").lower()
        if (needle in hay) if contains else (needle == hay):
            hits.append(idx)
    return hits


def apply_evidence(
    path: str | Path,
    *,
    title: str,
    target_stage: str,
    live_evidence_text: str,
    retirement_evidence_text: str = "",
    contains: bool = False,
    dry_run: bool = False,
) -> EvidenceResult:
    p = Path(path)
    errors: list[str] = []
    if target_stage not in {"live_verified", "retired"}:
        errors.append("target_stage must be live_verified or retired")
    if not live_evidence_text.strip():
        errors.append("live evidence is required")
    if target_stage == "retired" and not retirement_evidence_text.strip():
        errors.append("retirement evidence is required for retired")
    lines, records = load_lines(p)
    hits = match_records(records, title, contains)
    if not hits:
        errors.append("no matching Hermes record")
    if len(hits) > 1 and not contains:
        errors.append("multiple matching records; use a more specific title")
    if errors:
        return EvidenceResult(False, "apply", len(hits), 0, str(p), errors=errors)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = 0
    for idx in hits:
        record = records[idx]
        stage = lifecycle_stage(record)
        if target_stage == "live_verified" and stage not in {"mitigated", "live_verified"}:
            errors.append(f"record {idx} is {stage}, not mitigated/live_verified")
            continue
        if target_stage == "retired" and stage not in {"mitigated", "live_verified"}:
            errors.append(f"record {idx} is {stage}, not mitigated/live_verified")
            continue
        record["lifecycleStage"] = target_stage
        record["status"] = "retired" if target_stage == "retired" else "live_verified"
        record["liveEvidence"] = live_evidence_text.strip()
        record["liveVerifiedAt"] = record.get("liveVerifiedAt") or now
        if target_stage == "retired":
            record["retirementEvidence"] = retirement_evidence_text.strip()
            record["retiredAt"] = record.get("retiredAt") or now
        record["lifecycleUpdatedAt"] = now
        updated += 1

    if errors:
        return EvidenceResult(False, "apply", len(hits), updated, str(p), errors=errors)
    backup = ""
    if not dry_run:
        backup_path = p.with_suffix(p.suffix + f".{utc_stamp()}.bak")
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            shutil.copy2(p, backup_path)
            backup = str(backup_path)
        content = "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records) + "\n"
        p.write_text(content, encoding="utf-8")
    return EvidenceResult(True, "apply", len(hits), updated, str(p), backup=backup)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan/apply Hermes lifecycle live evidence updates")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--path", default=str(DEFAULT_LOG))
    p_plan.add_argument("--json", action="store_true")
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--path", default=str(DEFAULT_LOG))
    p_apply.add_argument("--title", required=True)
    p_apply.add_argument("--contains", action="store_true")
    p_apply.add_argument("--target-stage", choices=["live_verified", "retired"], required=True)
    p_apply.add_argument("--live-evidence", required=True)
    p_apply.add_argument("--retirement-evidence", default="")
    p_apply.add_argument("--dry-run", action="store_true")
    p_apply.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "plan":
        result = plan(args.path)
    else:
        result = apply_evidence(
            args.path,
            title=args.title,
            target_stage=args.target_stage,
            live_evidence_text=args.live_evidence,
            retirement_evidence_text=args.retirement_evidence,
            contains=args.contains,
            dry_run=args.dry_run,
        )
    payload = asdict(result)
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Hermes lifecycle evidence {result.action}: {'OK' if result.ok else 'FAIL'} matched={result.matched} updated={result.updated}")
        if result.backup:
            print(f"backup: {result.backup}")
        for error in result.errors:
            print(f"- ERROR {error}")
        for candidate in result.candidates[:20]:
            print(f"- {candidate.stage} {candidate.title}: live_missing={candidate.missing_for_live_verified}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
