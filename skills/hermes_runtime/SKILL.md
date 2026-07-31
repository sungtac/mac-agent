---
name: hermes-runtime
description: Inspect Hermes feedback lifecycle status, plan safe evidence collection, and generate read-only evidence tickets. Use this for recurring failures, blocked improvements, lifecycle gates, and retirement readiness checks. It must not retire records or perform runtime actions without explicit evidence and approval.
version: 0.1.0
---

# Hermes Runtime Skill

## when_to_use

Use this skill when an agent needs to inspect or manage the Hermes feedback loop safely.

Typical triggers:

- A failure, regression, repeated bug, or blocked improvement should become a Hermes candidate.
- A Hermes item is mitigated but not yet live-verified or retired.
- Core-four strict readiness fails because Hermes retirement evidence is missing.
- The user asks why an item is still open, blocked, or not retired.
- The agent needs a read-only evidence ticket before requesting approved live proof.

Do **not** use this skill to claim a fix is complete unless current evidence proves it.

## required_tools

- Python 3
- `skills/hermes_runtime/hermes_lifecycle_gate.py`
- `skills/hermes_runtime/hermes_active_resolution_plan.py`
- `skills/hermes_runtime/hermes_evidence_tickets.py`
- `skills/hermes_runtime/hermes_lifecycle_evidence.py`

## inputs

- `hermes_log`: optional path to a Hermes JSONL ledger. Default is the workspace Hermes feedback ledger.
- `item_title` or `index`: optional item selector for focused review.
- `live_evidence`: explicit runtime proof, messageId, or approved evidence text. Required before promotion.
- `retirement_window`: recurrence-free period required before retired promotion.

## outputs

- Lifecycle gate report: active, mitigated, live-verified, retired, blocked, and missing evidence counts.
- Active resolution plan: safe next actions, blocked actions, and required evidence.
- Evidence tickets: operator-friendly read-only checklist for gathering proof.
- Evidence update plan: only when explicit live evidence is provided and the user has approved mutation.

## usage

Check lifecycle gate status:

```bash
python3 "$HOME/mac-agent/skills/hermes_runtime/hermes_lifecycle_gate.py" --json
```

Plan safe next actions for active high-priority Hermes items:

```bash
python3 "$HOME/mac-agent/skills/hermes_runtime/hermes_active_resolution_plan.py" --json
```

Generate read-only evidence tickets:

```bash
python3 "$HOME/mac-agent/skills/hermes_runtime/hermes_evidence_tickets.py" --json
```

Inspect live-evidence candidates without mutating records:

```bash
python3 "$HOME/mac-agent/skills/hermes_runtime/hermes_lifecycle_evidence.py" --plan --json
```

## safety

- Default mode is read-only.
- Do not synthesize messageId, live evidence, runtime proof, or recurrence-free windows.
- Do not send Telegram messages/files, restart Gateway, refresh credentials, delete files, or perform host/runtime actions just to collect evidence.
- Do not promote an item to `live_verified` or `retired` without explicit evidence.
- Do not treat mitigation evidence as retirement evidence.
- Retirement requires mitigation evidence, live evidence/runtime proof, retirement evidence, and a recurrence-free window.
- Any ledger mutation must be explicitly approved by the user and backed up before rewrite.
- Secrets, tokens, credentials, and private message contents must not be copied into Hermes records.

## examples

```bash
python3 "$HOME/mac-agent/skills/hermes_runtime/hermes_lifecycle_gate.py" --json
```

```bash
python3 "$HOME/mac-agent/skills/hermes_runtime/hermes_active_resolution_plan.py" --json
```

## Quality follow-up

Run this after creating or changing this skill:

```bash
python3 scripts/skill_quality_audit.py --skill skills/hermes-runtime/SKILL.md --run-tests --json
```
