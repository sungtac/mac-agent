# Code Review Contract

## Canonical request

All of the following Korean phrases map to the same intent, code_review:

코드리뷰, 코드 리뷰, 코드 점검, 코드점검, 코드 품질 검사, 코드품질검사, 코드 품질검사, 코드품질 검사.

The request must resolve to one scope: diff, files, module, repo, or snippet. paths is required for files and module; snippet carries the supplied text and source label. If no scope is stated, use diff only when a concrete worktree diff exists.

## States

REVIEWED means the primary reviewer produced a report. AI_APPROVED means an independent verifier approved the same target SHA. CHANGES_REQUIRED means at least one verified blocker or an unmet required check. ESCALATED means the evidence is insufficient, providers disagree on a material issue, or a high-risk decision needs a human. SUPERSEDED means a newer SHA invalidated this result.

Allowed transition:

REQUESTED → REVIEWING → REVIEWED → AI_APPROVED | CHANGES_REQUIRED | ESCALATED → SUPERSEDED

An approval is valid only when approval.reviewed_head_sha == target.head_sha. A new commit, changed review path, changed requirement, or changed deterministic-check result supersedes the prior report.

## Provider roles

- Codex is the default primary reviewer. It inspects the target, runs or evaluates checks, and emits findings only.
- Antigravity is the independent approval verifier. It receives the target and evidence, not an unverified “looks good” instruction.
- Claude is an optional second opinion for high-risk or disputed reviews. It does not override a failed verifier by majority vote.

No provider may claim a test passed without an evidence reference. A provider/tool timeout, malformed output, missing target, or unknown commit is a review failure, not an approval.

## Automatic and manual triggers

Automatic review starts only at:

1. agent work completion;
2. PR ready-for-review;
3. green CI on the same head SHA.

Manual invocations accept full repository, module/file, diff, or pasted snippet scope. Do not trigger a full review on every edit or every push by default.

The local event adapter treats PR lifecycle events as waiting states and starts only when a successful CI/workflow event identifies the same pull request and head SHA. This prevents duplicate reviews and reviews of failing or stale commits.

## Report requirements

Every finding needs a stable id, severity, category, location, title, evidence, and remediation. The report records checks attempted and their actual status (passed, failed, not_run, or error). Missing evidence prevents AI_APPROVED.
