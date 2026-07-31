# OpenClaw workspace migration inventory

This inventory is a deletion-readiness boundary, not a deletion instruction.

## Never copy automatically

- `.env` files, tokens, OAuth material, and credential reports
- `state/`, `memory/`, and live event ledgers
- `openclaw-backups/`
- user projects and uploaded media
- `.venv/`, caches, generated artifacts, and logs

## Review separately

- OpenClaw skills and their focused tests
- Team OS contracts that can be made provider-neutral
- Jarvis routing and quota logic after dependency review

## Deletion preconditions

Deletion requires zero active runtime references, a verified recovery archive,
an owner-approved retention decision, and a quarantine observation window.
