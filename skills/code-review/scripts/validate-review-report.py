#!/usr/bin/env python3
"""Fail-closed structural validator for code-review reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


STATUSES = {"REVIEWED", "AI_APPROVED", "CHANGES_REQUIRED", "ESCALATED", "SUPERSEDED"}
SEVERITIES = {"blocker", "high", "medium", "low", "nit"}
CATEGORIES = {
    "correctness",
    "security",
    "performance",
    "robustness",
    "maintainability",
    "tooling",
    "scope",
}
CHECK_STATUSES = {"passed", "failed", "not_run", "error"}
REQUIRED_FINDING = {"id", "severity", "category", "location", "title", "evidence", "remediation"}


def validate_report(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if report.get("schema_version") != "edge_agent.code_review_report.v1":
        errors.append("schema_version must be edge_agent.code_review_report.v1")
    for key in ("review_id", "status", "target", "findings", "checks"):
        if key not in report:
            errors.append(f"missing top-level field: {key}")
    status = report.get("status")
    if status not in STATUSES:
        errors.append(f"invalid status: {status}")
    target = report.get("target")
    if not isinstance(target, dict):
        errors.append("target must be an object")
    else:
        if target.get("scope") not in {"diff", "files", "module", "repo", "snippet"}:
            errors.append("target.scope is invalid")
        if not isinstance(target.get("head_sha"), str) or not target["head_sha"]:
            errors.append("target.head_sha is required")
        if target.get("scope") in {"files", "module"} and not target.get("paths"):
            errors.append("target.paths is required for files/module scope")
    findings = report.get("findings")
    if not isinstance(findings, list):
        errors.append("findings must be an array")
        findings = []
    blockers = False
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f"findings[{index}] must be an object")
            continue
        missing = REQUIRED_FINDING - finding.keys()
        errors.extend(f"findings[{index}] missing {key}" for key in sorted(missing))
        if finding.get("severity") not in SEVERITIES:
            errors.append(f"findings[{index}].severity is invalid")
        if finding.get("category") not in CATEGORIES:
            errors.append(f"findings[{index}].category is invalid")
        if finding.get("severity") == "blocker":
            blockers = True
        for key in REQUIRED_FINDING - {"severity"}:
            if key in finding and (not isinstance(finding[key], str) or not finding[key].strip()):
                errors.append(f"findings[{index}].{key} must be non-empty")
    checks = report.get("checks")
    if not isinstance(checks, list):
        errors.append("checks must be an array")
        checks = []
    check_failure = False
    for index, check in enumerate(checks):
        if not isinstance(check, dict) or not check.get("name"):
            errors.append(f"checks[{index}] must have a name")
            continue
        if check.get("status") not in CHECK_STATUSES:
            errors.append(f"checks[{index}].status is invalid")
        if check.get("status") in {"failed", "error", "not_run"}:
            check_failure = True
    if status == "AI_APPROVED":
        approval = report.get("approval")
        if not isinstance(approval, dict):
            errors.append("AI_APPROVED requires approval")
        else:
            for key in ("provider", "reviewed_head_sha", "decision_reason"):
                if not approval.get(key):
                    errors.append(f"approval.{key} is required")
            if approval.get("reviewed_head_sha") != (target or {}).get("head_sha"):
                errors.append("approval reviewed_head_sha does not match target head_sha")
        if blockers:
            errors.append("AI_APPROVED cannot contain blocker findings")
        if not checks:
            errors.append("AI_APPROVED requires at least one passed check")
        elif check_failure:
            errors.append("AI_APPROVED requires all checks to be passed")
    if status in {"REVIEWED", "AI_APPROVED"} and any(
        finding.get("severity") == "blocker" and not finding.get("verified", False)
        for finding in findings if isinstance(finding, dict)
    ):
        errors.append("blocker findings must be verified before approval decisions")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", nargs="?", help="JSON report path; stdin is used when omitted")
    args = parser.parse_args()
    try:
        raw = Path(args.report).read_text(encoding="utf-8") if args.report else sys.stdin.read()
        report = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False))
        return 1
    errors = validate_report(report)
    print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
