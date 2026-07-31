# Event Trigger Contract

The event adapter is pure and credential-free. It does not call GitHub, merge a PR, or invoke a model.

For a real webhook receiver, verify the raw request body with the
X-Hub-Signature-256 HMAC header before parsing JSON. Configure the secret
through the process environment; never put it in a payload, command argument,
review report, or log.

The executable bridge is:

    node bin/code-review-event-bridge.js <event-name> <payload.json|-> [--delivery-id ID] [--signature SIG] [--secret-env ENV] [--require-signature]

It emits one JSON decision object and exits zero for valid events, including
ignore and await_ci decisions. Malformed input exits non-zero and does not
print the payload.

When a secret is configured, signature verification is required. Exit code 3
means signature verification failed; the payload is not parsed or forwarded.

For a separately managed local HTTP receiver, run:

    CODE_REVIEW_WEBHOOK_SECRET=WEBHOOK_SECRET node bin/code-review-webhook-server.js

It binds to 127.0.0.1:8787 and accepts POST /github/webhook by default.
CODE_REVIEW_WEBHOOK_HOST and CODE_REVIEW_WEBHOOK_PORT may change the bind
address. The server refuses to start without CODE_REVIEW_WEBHOOK_SECRET and
does not invoke a provider. Successful start decisions are durably written as
normalized requests below the configured queue root (by default
~/.edge-agent/state/code-review/requests); a separate worker consumes pending
requests. The raw webhook payload is never written to the queue.

The queue consumer is:

    node bin/code-review-request-worker.js --repository-root /path/to/repo --repository-name OWNER/REPO --isolated

It performs a dry-run by default. `--execute` is required to call Codex and
Antigravity. Without `--isolated`, it requires the configured repository
identity, a clean worktree, and an exact local HEAD match to the queued
head_sha. With `--isolated`, it verifies the repository remote, fetches the
requested target SHA when needed, and reviews it in a temporary detached
worktree without changing the source worktree. A provider failure leaves the
request pending for retry; only a persisted report is followed by queue
completion.

For periodic operation, use the allowlisted runner:

    node bin/code-review-worker-runner.js --config config/code-review-repositories.json

`config/com.macagent.code-review-worker.plist.template` is the configured
launchd template and includes the explicit `--execute` opt-in. The standalone
worker command remains dry-run unless `--execute` is passed. Run the preflight
with `--allow-execute` when intentionally enabling provider calls.

## Accepted events

- pull_request with opened, reopened, ready_for_review, or synchronize: record the target and wait for CI.
- workflow_run with action=completed and conclusion=success: start review for the workflow pull request and head_sha.
- check_suite with action=completed and conclusion=success: start review for the check suite pull request and head_sha.

Draft pull requests, failed or incomplete CI, unsupported actions, missing repository identity, missing PR number, and missing SHA fail closed to ignore or await_ci; they never start a review.

## Idempotency

The review identity is repository#pull_request@head_sha, hashed for transport. Re-delivering the same webhook resolves to the same key. A new commit produces a new key and requires a new review and approval.

## Persistence

Reports are stored below the Edge Agent runtime state root, normally ~/.edge-agent/state/code-review. Each report is immutable and keyed by review_id + target.head_sha; a separate latest pointer is only an index. The store uses atomic replacement and restrictive local file permissions.
