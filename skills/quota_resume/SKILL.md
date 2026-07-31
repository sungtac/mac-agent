---
name: quota_resume
description: Safely review token quota, rate-limit, resume queue, context packet, and fallback switch preview workflows; use for quota exhaustion, 429/rate_limit, resume after recharge, fallback approval, Hermes quota events, and user-review-gated recovery.
---

# quota_resume Skill

Use this skill when OpenClaw/Sukja needs to review or prepare recovery from token quota, rate-limit, recharge, paused autoloop, resume queue, Hermes quota events, or fallback switching.

This skill is **preview-only by default**. It prepares reviewable state and JSON evidence. It must not automatically switch accounts, resume work, send external messages, read raw OAuth/token/cookie files, or claim live quota-exhaustion failover.

## When to use

Use this skill for:

- quota exhaustion or suspected `429` / `rate_limit` / `insufficient_quota` events
- resume queue preview after recharge
- Hermes quota event recording
- fallback switch preview and approval review
- checking whether quota recovery actions remain user-review-gated
- building context packets for a future resume, without executing the resume automatically

Do not use this skill to perform real account switching or external notification unless a separate user approval path explicitly authorizes that action.

## Supported source files

Current implementation files:

- `quota_resume.py`
- `quota_resume_wrapper.py`
- `fallback_switch_preview.py`
- `hermes_quota_event_hook.py`
- `runtime_flags.py`
- `supervisor_checkpoint_hook.py`
- `telegram_notification_candidate.py`

## Entry points and safe workflows

### Initialize or record quota state

`quota_resume.py` manages local state files, quota events, active task metadata, and resume queue records. It uses locked/atomic JSON writes and redacts sensitive-looking values.

Example preview-safe CLI surface:

```bash
python3 skills/quota_resume/quota_resume.py .
python3 skills/quota_resume/quota_resume.py . --record-event '{"type":"rate_limit","message":"429 rate_limit"}'
```

### Build resume previews

`quota_resume_wrapper.py` builds reviewable resume previews and context packets from ready queue items. It sets `auto_execute: false` and `requires_user_review: true`.

Use this only to prepare evidence for review, not to resume the task automatically.

### Record Hermes quota events

`hermes_quota_event_hook.py` records quota-like Hermes events, adds resume queue items, and creates fallback preview state. It must preserve:

- `auto_execute: false`
- `requires_user_review: true`

### Fallback switch preview

`fallback_switch_preview.py` can build and save fallback-switch previews. Any apply path must be treated as approval-gated and must not be invoked casually from this skill.

Before applying a fallback switch, require a separate explicit user approval and report the planned target/source. Do not imply that preview means applied.

### Runtime safety flags

`runtime_flags.py` must keep quota resume auto-execution disabled:

- `quota_resume_auto_execute: false`
- `requires_user_review: true`

### Notification candidates

`telegram_notification_candidate.py` may prepare candidate notification content, but this skill must not send external messages by itself.

## Safety rules

1. Do not read raw OAuth/access/refresh token/cookie files.
2. Do not disclose secrets, tokens, cookies, API keys, or raw auth state.
3. Do not perform a real account switch unless a separate explicit approval path authorizes it.
4. Do not claim actual live quota-exhaustion failover unless real runtime evidence exists.
5. Do not send Telegram or other external notifications from this skill without explicit approval and delivery evidence.
6. Do not restart Gateway, mutate OpenClaw auth config, or run live quota-burning probes from this skill.
7. Prefer JSON previews and state evidence over verbal claims.
8. Treat missing processed probes as `unmeasured`, not as quota success/failure.

## Outputs and evidence

Acceptable evidence includes:

- JSON result showing `status: success`
- preview objects showing `auto_execute: false`
- preview objects showing `requires_user_review: true`
- context packet paths generated for later review
- safe masked account suffixes only, never raw credentials
- local test output proving preview/atomic behavior

## Tests / quality gate

Run these after changing this skill or its implementation:

```bash
python3 scripts/skill_quality_audit.py --skill skills/quota_resume --json
python3 -m py_compile skills/quota_resume/*.py scripts/skill_quality_audit.py
python3 jarvis/test_sail_33_01_hermes_resume_queue.py
python3 jarvis/test_sail_35_01_fallback_switch_approval.py
python3 jarvis/test_sail_62_01_quota_resume_atomic_writes.py
```

## Current limitations

- Skill-local tests under `skills/quota_resume/tests/` are not restored yet.
- Some stale `__pycache__` entries reference older proactive preview modules whose source files are not present. Do not delete or clean cache artifacts until workspace hygiene policy and user approval are in place.
- This skill documents preview/readiness behavior only; it does not prove live quota-exhaustion failover.

## Quality follow-up

Run `python3 scripts/skill_quality_audit.py --skill <this-skill> --run-tests --json` after creating or changing this skill.
