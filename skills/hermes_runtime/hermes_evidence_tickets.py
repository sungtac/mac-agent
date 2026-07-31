#!/usr/bin/env python3
"""Generate read-only evidence tickets for active Hermes blockers.

This turns the active resolution planner output into operator-friendly tickets.
It does not queue probes, send messages/files, restart Gateway, delete files, or
mutate the Hermes ledger.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class EvidenceTicket:
    id: str
    source_index: int | None
    title: str
    status: str
    priority: int
    reason: str
    safe_next_actions: list[str]
    blocked_actions: list[str]
    required_evidence: list[str]
    promotion_target: str = "live_verified"
    mutation_policy: str = "read_only_until_evidence_is_attached"


def slugify(text: str) -> str:
    keep = []
    for ch in text.lower():
        if ch.isalnum():
            keep.append(ch)
        elif keep and keep[-1] != "-":
            keep.append("-")
    return "".join(keep).strip("-")[:80] or "ticket"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_active_plan(path: Path | None = None) -> dict[str, Any]:
    if path:
        return json.loads(path.read_text(encoding="utf-8"))
    proc = subprocess.run(
        [sys.executable, "-m", "skills.hermes_runtime.hermes_active_resolution_plan", "--json"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        return {"ok": False, "items": [], "error": proc.stderr[-4000:], "returncode": proc.returncode}
    payload = json.loads(proc.stdout)
    payload["returncode"] = proc.returncode
    return payload


def tickets_from_plan(plan: dict[str, Any]) -> list[EvidenceTicket]:
    tickets: list[EvidenceTicket] = []
    for item in plan.get("items", []):
        title = str(item.get("title") or "Hermes active blocker")
        tickets.append(
            EvidenceTicket(
                id=f"hermes-evidence-{item.get('index', 'x')}-{slugify(title)}",
                source_index=item.get("index"),
                title=title,
                status="blocked" if item.get("blocked_actions") else "approval_required",
                priority=int(item.get("priority") or 0),
                reason=str(item.get("reason_active") or "missing live/runtime evidence"),
                safe_next_actions=list(item.get("safe_next_actions") or []),
                blocked_actions=list(item.get("blocked_actions") or []),
                required_evidence=list(item.get("required_evidence") or []),
            )
        )
    return tickets


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Hermes Evidence Tickets",
        "",
        f"- generated_at: {payload['generated_at']}",
        f"- ok: {payload['ok']}",
        f"- ticket_count: {payload['ticket_count']}",
        "- mutation_policy: no Telegram send, no Gateway restart, no deletion, no Hermes ledger mutation",
        "",
    ]
    for ticket in payload["tickets"]:
        lines.extend([
            f"## {ticket['id']}",
            f"- title: {ticket['title']}",
            f"- status: {ticket['status']}",
            f"- priority: {ticket['priority']}",
            f"- reason: {ticket['reason']}",
            "- required_evidence:",
        ])
        lines.extend(f"  - {item}" for item in ticket["required_evidence"] or ["-"])
        lines.append("- safe_next_actions:")
        lines.extend(f"  - {item}" for item in ticket["safe_next_actions"] or ["-"])
        lines.append("- blocked_actions:")
        lines.extend(f"  - {item}" for item in ticket["blocked_actions"] or ["-"])
        lines.append("")
    return "\n".join(lines)


def build_payload(plan: dict[str, Any]) -> dict[str, Any]:
    tickets = tickets_from_plan(plan)
    return {
        "schema": "openclaw.hermes_evidence_tickets.v1",
        "ok": not plan.get("error"),
        "generated_at": utc_now(),
        "source": "scripts/hermes_active_resolution_plan.py --json",
        "source_ok": plan.get("ok"),
        "ticket_count": len(tickets),
        "tickets": [asdict(ticket) for ticket in tickets],
        "limits": [
            "Read-only ticket generator.",
            "Tickets are not live evidence and do not justify retired promotion.",
            "External/runtime actions require explicit approval and successful artifacts.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate read-only Hermes evidence tickets")
    parser.add_argument("--input", default="", help="Use an existing active_resolution.json instead of running the planner")
    parser.add_argument("--output-dir", default=str(ROOT / "state" / "hermes-evidence-tickets"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    plan = load_active_plan(Path(args.input) if args.input else None)
    payload = build_payload(plan)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "latest.md").write_text(render_markdown(payload), encoding="utf-8")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(payload))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
