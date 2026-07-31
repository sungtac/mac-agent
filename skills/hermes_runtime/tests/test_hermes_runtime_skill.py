#!/usr/bin/env python3
"""Minimal safety/readiness test for the hermes-runtime skill document."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "skills" / "hermes_runtime" / "SKILL.md"
REQUIRED_REFERENCES = [
    ROOT / "skills" / "hermes_runtime" / "hermes_lifecycle_gate.py",
    ROOT / "skills" / "hermes_runtime" / "hermes_active_resolution_plan.py",
    ROOT / "skills" / "hermes_runtime" / "hermes_evidence_tickets.py",
    ROOT / "skills" / "hermes_runtime" / "hermes_lifecycle_evidence.py",
]
REQUIRED_PHRASES = [
    "Default mode is read-only",
    "Do not synthesize messageId",
    "Do not send Telegram messages/files",
    "recurrence-free window",
    "explicitly approved by the user",
]


def test_skill_document_references_existing_safe_helpers() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for path in REQUIRED_REFERENCES:
        assert path.exists(), path
        assert path.name in text


def test_skill_document_keeps_retirement_safety_rules() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for phrase in REQUIRED_PHRASES:
        assert phrase in text


def main() -> int:
    test_skill_document_references_existing_safe_helpers()
    test_skill_document_keeps_retirement_safety_rules()
    print("PASS: hermes-runtime skill tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
