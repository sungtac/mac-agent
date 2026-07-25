# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Source of truth for Claude Code assets (skills, workflows) shared across a fleet of agents. Each agent pulls this repo in (symlink into `~/.claude/` for personal use, or via a plugin marketplace manifest for shared use — marketplace setup is still pending).

## Structure

This is an index only — each entry is a one/two-line pointer. Full rationale, known bugs, and porting notes live in `docs/*.md`. Read the linked doc before touching that script; don't assume the one-liner here is the whole story.

## Contents

- `setup.sh` — run this first on any new agent/machine. Checks Codex CLI and Antigravity CLI (`agy`), installs via `brew install --cask` if missing, then guides login (OAuth can't be done headlessly — a human has to approve in a browser).

- `workflows/verify-task.js` — independent-verification Workflow: scores a completed task with both Codex and Antigravity/Gemini against a shared rubric, revises on failure, loops until it passes. → [docs/verify-task.md](docs/verify-task.md)
- `workflows/verify-task-v2.js` — pre-execution spec-lock + tiered (light/full) multi-agent review, author-never-grades-self enforced per track. → [docs/verify-task-v2-design.md](docs/verify-task-v2-design.md)
- `hooks/verify-task-stop-check.sh` — Stop hook that nags (once per session) if substantive work happened without a `verify-task` run. → [docs/verify-task-stop-check.md](docs/verify-task-stop-check.md)
- `workflows/lib/coach-headroom.sh` / `usage-advisor.sh` / `route-dispatch.sh` — usage-balancing routing per Rule A ("Orchestrator 내부 추론 우선") / Rule B ("안티그래비티는 명시적 트리거 시만"): codex-capable work compares codex vs Claude headroom (advisor, not fully enforceable — "prefer claude" just means the session proceeds itself), simple work tries antigravity then falls back to codex (dispatch, fully enforced). Not used by verify-task(-v2)'s fixed author/grader role assignments. → [docs/usage-routing.md](docs/usage-routing.md)
- `hooks/usage-routing-check.sh` — Stop hook that nags (once per session) if Claude did substantial direct work while Claude's own `coach` usage level was yellow/red with no sign of consulting the routing policy. → [docs/usage-routing.md](docs/usage-routing.md)

### 일정비서 (work/calendar secretary)

Three pieces that together turn finished work (Claude Code sessions, shared recordings, shared documents) into an archived record + calendar entry, plus a Friday rollup:

- `hooks/work-log-stop-check.sh` — Stop hook: classifies each session as public-work vs. meta/private, archives output files + logs a Calendar event for public-work sessions. → [docs/worklog-hook.md](docs/worklog-hook.md)
- `cron/weekly-report.sh` — Thursday 18:00 `launchd` job: compiles the week's Calendar entries into a "이번 주 한 일 / 다음 주 할 일" report, saved to Drive + logged as a Friday Calendar event. Headless `claude -p` invocations hang intermittently under launchd; mitigated with a watchdog + 3x retry. → [docs/weekly-report.md](docs/weekly-report.md)
- `bin/transcribe.sh` — local, offline Whisper transcription for shared audio recordings; feeds into the same archive+calendar flow as documents. → [docs/transcribe.md](docs/transcribe.md)
- `bin/discord-bot.py` — persistent (launchd `KeepAlive`) Discord bot: `!주간보고서` / `!상태` on-demand triggers, channel-scoped. `bin/discord-notify.sh` — one-way fire-and-forget notify helper (used by weekly-report.sh, work-log-stop-check.sh, verify-task-v2.js escalations), works independently of the bot process. → [docs/discord-bot.md](docs/discord-bot.md)

Also relevant: the Google Drive MCP connector is permanently blocked (`permissions.deny` in `~/.claude/settings.json`) — it's authenticated as a third party's account, not the user's own. Details in [docs/weekly-report.md](docs/weekly-report.md).

Update this file (and the matching `docs/*.md`) as more skills/workflows are added. Keep entries here to 1-2 lines — push narrative detail into the linked doc.
