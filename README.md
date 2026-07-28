# mac-agent

Source of truth for Claude Code assets (hooks, workflows, scripts) shared across a fleet of agents on this Mac. Each agent pulls this repo in — symlink into `~/.claude/` for personal use, or via a plugin marketplace manifest for shared use (marketplace setup still pending).

Full index of what's here, what each piece does, and known bugs/limitations: **[CLAUDE.md](CLAUDE.md)**. Narrative detail, rationale, and porting notes for each individual script live under [docs/](docs/).

Setting up a brand-new agent/machine (this repo + Claude Skills + document-writing skills)? Start with **[docs/new-agent-setup.md](docs/new-agent-setup.md)**.

## Quick orientation

- `setup.sh` — run this first on any new agent/machine (checks/installs the Codex and Antigravity CLIs).
- `workflows/`, `hooks/` — independent-verification workflow (`verify-task`) and its enforcing Stop hook.
- `hooks/work-log-stop-check.sh`, `cron/weekly-report.sh`, `bin/transcribe.sh` — 일정비서 (work/calendar secretary): archives finished work to Google Drive + Calendar automatically, with a Friday rollup report and offline recording transcription.

See [CLAUDE.md](CLAUDE.md) for the full picture before touching any script here.
