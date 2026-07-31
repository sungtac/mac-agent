# Edge Agent skill integration contract

## Canonical boundary

- Source repository: `/Users/edge_ai/mac-agent`
- Runtime state: `~/.edge-agent/state`
- Task workspaces: `~/.edge-agent-worktrees/`
- Legacy OpenClaw workspace: quarantine only; never an active skill root

## Skill shape

Each portable skill must contain:

- `SKILL.md`: triggers, inputs, outputs, safety rules, and limitations
- focused helper modules under the skill directory
- tests that do not require credentials or external sends
- no hard-coded OpenClaw workspace paths

Code-review state follows the same boundary: immutable reports are stored under
the configured Edge Agent state root, normally ~/.edge-agent/state/code-review.
PR event handling is a pure local decision adapter; GitHub registration and
provider invocation remain separate runtime responsibilities. The
code-review-event-bridge command is the credential-free process boundary for
GitHub Actions or a separately managed webhook receiver. A webhook receiver
must verify the raw-body HMAC signature before invoking the bridge and must
keep the secret outside runtime reports and logs.

The local HTTP receiver additionally persists only the normalized,
SHA-bound review request to the code-review request queue before returning an
accepted start decision. Queue writes are atomic and idempotent; the raw
webhook payload is not persisted. Provider execution remains a separate
worker responsibility.

The reference worker is `bin/code-review-request-worker.js`. The standalone
command defaults to dry-run; the launchd runner may pass `--execute` only after
the configured operation has explicitly enabled it. Clean/isolated-worktree and
exact-head-SHA checks still apply. Provider failures leave the request pending;
a queue item is completed only after the SHA-bound report is stored.

Repository routing is explicit through
`config/code-review-repositories.json`; unknown repositories are not guessed
or executed. Launchd is provided as the explicit template
`config/com.macagent.code-review-worker.plist.template`; an operator must run
the provider preflight with `--allow-execute` after confirming provider cost and
repository access before installing or loading it.

## Promotion gates

Static quality, reference resolution, unit tests, connector verification, and
runtime canary evidence are separate gates. Passing a static audit alone does
not prove live behavior.
