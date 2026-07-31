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

## Promotion gates

Static quality, reference resolution, unit tests, connector verification, and
runtime canary evidence are separate gates. Passing a static audit alone does
not prove live behavior.
