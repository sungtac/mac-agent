# Edge Agent domain-skill integration contract

This directory is the migration boundary for former OpenClaw domain skills.

## Portable candidates

- `quota_resume`: preview and user-review gates only; no automatic resume.
- `product-research`: public sources, price uncertainty, and citation output.
- `roda-public-search`: bounded public search with privacy and disambiguation
  controls.
- `calendar`: explicit date/time confirmation and isolated OAuth handling.

## Rules

1. A domain skill may not import OpenClaw modules or write to retired OpenClaw
   paths; active state belongs under the Edge Agent runtime contract.
2. Persistent state uses the Edge Agent runtime state contract.
3. External sends, deletions, account changes, and fallback switches remain
   user-review gated.
4. Every migrated helper requires a focused test and a reference audit.
