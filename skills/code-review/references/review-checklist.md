# Review Checklist

Use this as a prioritised checklist, not as a reason to invent findings.

## Correctness

- Does the implementation satisfy the requested behaviour and existing invariants?
- Are error, null, empty, retry, timeout, and partial-success paths handled?
- Are state transitions, idempotency, ordering, and rollback behaviour correct?
- Are tests asserting the changed behaviour rather than only execution?

## Security and privacy

- Are authentication, authorization, tenant boundaries, secrets, and personal data protected?
- Are inputs validated at trust boundaries and safely parameterized/escaped?
- Could logs, errors, generated artifacts, or external calls disclose sensitive data?
- Are dependency, serialization, file, shell, and prompt-injection boundaries safe?

## Robustness and performance

- Are concurrency, race, cancellation, retry, and resource cleanup paths safe?
- Is work bounded for large inputs and hostile payloads?
- Are database queries, network calls, loops, and caches appropriate under load?

## Maintainability

- Is the change smaller and simpler than necessary?
- Does it preserve public contracts and backward compatibility?
- Are names, error messages, documentation, and tests consistent with the repository?

## Evidence standard

Report exact file/line or snippet location, the observed path to failure, and the check or reasoning that supports it. Mark uncertainty explicitly. Existing issues unrelated to the change are informational and must not be presented as newly introduced blockers.
