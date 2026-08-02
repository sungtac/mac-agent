---
name: harness-memory
description: Search and record troubleshooting memory so agents can reuse verified fixes and avoid repeated failed attempts. Use this before debugging, testing, retries, regressions, or repeated operational issues.
version: 1.1.0
---

# Harness Memory Skill

## when_to_use

Use this skill before starting a debugging, test-repair, regression-recovery, or operational troubleshooting task.

Typical triggers:

- A command, test, build, Telegram flow, HWPX flow, Gateway helper, or Team OS check failed.
- The same error or similar symptom may have happened before (Search first).
- A fix succeeded and should be recorded for future reuse (Save success).
- A fix failed and should be recorded so agents do not repeat it (Save failure).
- A long task is being resumed and prior evidence should be checked first.
- **ANTI-LOOP RULE**: If an agent encounters the same error 2 times, it MUST record the failure here and search for alternatives instead of blindly retrying.

Do **not** use this skill as proof that a current issue is fixed. It is memory, not live verification.

## required_tools

- Python 3
- `skills/harness-memory/harness_memory.py`
- The memory store is created on demand under the workspace state directory.

## inputs

- `query`: keywords that describe the current situation, error, component, or symptom.
- `date`: date of the success or failure record.
- `situation`: short description of the problem.
- `steps_json_array`: JSON array of successful steps.
- `attempted_steps_json_array`: JSON array of failed attempted steps.
- `result`: short success result.
- `failure_reason`: short reason the attempt failed.

## outputs

Search/query returns:

- `NO_MATCH`: no useful prior memory found.
- JSON with `count` and matching records when prior memory exists.

Record commands return:

- `RECORDED_SUCCESS`: successful procedure was stored.
- `RECORDED_FAIL`: failed attempt was stored.

## usage

Search prior troubleshooting memory:

```bash
python3 skills/harness-memory/harness_memory.py search "<situation keywords>"
```

`query` is an alias of `search`:

```bash
python3 skills/harness-memory/harness_memory.py query "<situation keywords>"
```

Record a successful procedure:

```bash
python3 skills/harness-memory/harness_memory.py add_success \
  "<date>" \
  "<situation>" \
  '["step 1", "step 2"]' \
  "<result>"
```

Record a failed attempt:

```bash
python3 skills/harness-memory/harness_memory.py add_fail \
  "<date>" \
  "<situation>" \
  '["attempted step 1", "attempted step 2"]' \
  "<failure_reason>"
```

## safety

- This skill stores and retrieves troubleshooting notes only. It does not prove the current runtime is healthy.
- Always run current verification after applying a remembered fix.
- Do not store secrets, tokens, passwords, raw credentials, private URLs, or sensitive message contents.
- Do not use remembered steps to bypass user approval. External sends, service restarts, credential access, deletion, elevated host actions, and runtime changes still require approval.
- Prefer recording minimal reproducible steps and evidence paths instead of long logs.
- If memory contradicts current test results, trust the current test result and record the new outcome.
- **Strict Anti-Loop**: If you see a `RECORDED_FAIL` for a step you were about to try, you MUST NOT try it. If you fail at a step, immediately add a `RECORDED_FAIL` before asking for human help or retrying wildly.

## examples

```bash
python3 skills/harness-memory/harness_memory.py search "skill quality audit missing reference"
```

```bash
python3 skills/harness-memory/harness_memory.py add_success \
  "2026-06-26" \
  "command-registry SKILL.md missing reference" \
  '["replace stale absolute path", "run the repository skill test command"]' \
  "repository skill tests passed"
```

## Quality follow-up

Run this after creating or changing this skill:

```bash
PYTHONPATH=. python3 skills/harness-memory/tests/test_harness_memory.py
```
