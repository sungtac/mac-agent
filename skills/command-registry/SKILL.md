---
name: command-registry
description: Validate a shell command string before execution and remember known-good or blocked command alternatives. Use this before risky, unfamiliar, repeated, or previously failing commands. This skill never executes the command itself.
version: 1.1.0
---

# Command Registry Skill

## when_to_use

Use this skill when an agent or harness is about to run a shell command and needs to know whether the command is already verified, unknown, or blocked.

Typical triggers:

- Before running a risky, unfamiliar, destructive, privileged, restart, network, or host/runtime-changing command.
- Before retrying a command that failed earlier.
- When a safer replacement command may exist.
- After a command succeeds and should be remembered as a verified command.
- After a command fails and should be recorded with a safer alternative.

Do **not** use this skill to execute commands. It only validates and records command strings.

## required_tools

- Python 3
- `skills/command-registry/command_registry.py` (기본 상태 경로는 `~/.edge-agent/state/skills/`)
- Local record file is created on demand under the Edge Agent state directory.

## inputs

- `command`: the exact command string to check or record.
- `failure_reason`: short reason a failed command should not be retried as-is.
- `replacement_command`: safer alternative command string, if known.

## outputs

The CLI prints one of these values:

- `VALID`: command is known-good.
- `UNKNOWN`: command is not known yet; the agent must apply normal safety review before running it.
- `BLACKLISTED -> 대체: <command>`: do not run the original command; consider the replacement.
- `RECORDED_SUCCESS`: success record saved.
- `RECORDED_FAIL`: failure/blacklist record saved.

## usage

Check a command:

```bash
python3 skills/command-registry/command_registry.py check "<command>"
```

Record a successful command:

```bash
python3 skills/command-registry/command_registry.py update_success "<command>"
```

Record a failed command and safer replacement:

```bash
python3 skills/command-registry/command_registry.py update_fail "<failed_command>" "<failure_reason>" "<replacement_command>"
```

## safety

- This skill must never execute the command string.
- `VALID` does not override user approval requirements. External sends, service restarts, credential access, deletion, elevated host actions, and runtime changes still require the normal approval gate.
- `UNKNOWN` is not permission to run. It means the agent must inspect the command, scope, and approval requirement first.
- Do not store secrets, tokens, passwords, API keys, or raw credentials in command strings or reasons.
- Prefer non-mutating dry-run/status commands when available.
- If the command is blocked, do not retry the original command repeatedly; use the replacement only after normal safety review.

## examples

```bash
python3 skills/command-registry/command_registry.py check "python3 -m py_compile skills/command-registry/command_registry.py"
```

```bash
python3 skills/command-registry/command_registry.py update_fail \
  "openclaw gateway restart" \
  "direct runtime restart requires approval" \
  "scripts/safe_gateway_restart.sh --approved <ticket>"
```

## Quality follow-up

Run this after creating or changing this skill:

```bash
PYTHONPATH=. python3 skills/command-registry/tests/test_command_registry.py
```
