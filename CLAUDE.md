# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Source of truth for Claude Code assets (skills, workflows) shared across a fleet of agents. Each agent pulls this repo in (symlink into `~/.claude/` for personal use, or via a plugin marketplace manifest for shared use — marketplace setup is still pending).

## Contents

- `setup.sh` — run this first on any new agent/machine. Checks Codex CLI and Antigravity CLI (`agy`) login status and guides you through logging in (drives the login directly if run in a real interactive terminal; otherwise prints the exact command to run yourself). OAuth logins can't be done silently/headlessly — a human has to approve in a browser — so this only streamlines the check + guidance, not a magic no-touch login.
- `workflows/verify-task.js` — a Claude Code Workflow script. Given a completed task (`task`, `result`, optional `persona`/`cwd`/`maxRounds`), it first runs a preflight check (Codex/Antigravity login status — fails fast with guidance to `setup.sh` if either is missing), then independently scores the result with both Codex (`codex exec`) and Antigravity/Gemini (`agy -p`) against a shared 100-point rubric (pass at 85+, with a dealbreaker rule for critical categories), revises on failure, and loops until it passes or `maxRounds` is hit. Full rubric lives in the script's `RUBRIC` constant.
  - **Prerequisites on the machine running it:** Codex CLI (`codex`, logged in) and Antigravity CLI (`agy`, logged in) both on PATH — run `setup.sh` to check/fix. The script currently hardcodes the Antigravity binary at `/Users/edge_ai/.local/bin/agy` — adjust that path if installed elsewhere.
  - Usage: `Workflow({scriptPath: "workflows/verify-task.js", args: {task, result, persona, cwd, maxRounds}})`, or copy/symlink it into `~/.claude/workflows/` to invoke by `name: "verify-task"`.

Update this file as more skills/workflows are added.
