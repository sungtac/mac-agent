# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Source of truth for Claude Code assets (skills, workflows) shared across a fleet of agents. Each agent pulls this repo in (symlink into `~/.claude/` for personal use, or via a plugin marketplace manifest for shared use — marketplace setup is still pending).

## Structure

This is an index only — each entry is a one/two-line pointer. Full rationale, known bugs, and porting notes live in `docs/*.md`. Read the linked doc before touching that script; don't assume the one-liner here is the whole story.

## Contents

- `setup.sh` — run this first on any new agent/machine. Checks Codex CLI and Antigravity CLI (`agy`), installs via `brew install --cask` if missing, then guides login (OAuth can't be done headlessly — a human has to approve in a browser).

- `workflows/verify-task.js` — independent-verification Workflow: scores a completed task with both Codex and Antigravity/Gemini against a shared rubric, revises on failure, loops until it passes. → [docs/verify-task.md](docs/verify-task.md)
- `hooks/verify-task-stop-check.sh` — Stop hook that nags (once per session) if substantive work happened without a `verify-task` run. → [docs/verify-task-stop-check.md](docs/verify-task-stop-check.md)

### 일정비서 (work/calendar secretary)

Three pieces that together turn finished work (Claude Code sessions, shared recordings, shared documents) into an archived record + calendar entry, plus a Friday rollup:

- `hooks/work-log-stop-check.sh` — Stop hook: classifies each session as public-work vs. meta/private, archives output files + logs a Calendar event for public-work sessions. → [docs/worklog-hook.md](docs/worklog-hook.md)
- `cron/weekly-report.sh` — Friday `launchd` job: compiles the week's Calendar entries into a "이번 주 한 일 / 다음 주 할 일" report. **Has an open, unresolved hang bug under real launchd triggering** — check this before trusting it ran. → [docs/weekly-report.md](docs/weekly-report.md)
- `bin/transcribe.sh` — local, offline Whisper transcription for shared audio recordings; feeds into the same archive+calendar flow as documents. → [docs/transcribe.md](docs/transcribe.md)

Also relevant: the Google Drive MCP connector is permanently blocked (`permissions.deny` in `~/.claude/settings.json`) — it's authenticated as a third party's account, not the user's own. Details in [docs/weekly-report.md](docs/weekly-report.md).

Update this file (and the matching `docs/*.md`) as more skills/workflows are added. Keep entries here to 1-2 lines — push narrative detail into the linked doc.
