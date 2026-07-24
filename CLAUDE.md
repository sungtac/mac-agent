# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Source of truth for Claude Code assets (skills, workflows) shared across a fleet of agents. Each agent pulls this repo in (symlink into `~/.claude/` for personal use, or via a plugin marketplace manifest for shared use — marketplace setup is still pending).

## Contents

- `workflows/verify-task.js` — a Claude Code Workflow script. Given a completed task (`task`, `result`, optional `persona`/`cwd`/`maxRounds`), it independently scores the result with both Codex (`codex exec`) and Antigravity/Gemini (`agy -p`) against a shared 100-point rubric (pass at 85+, with a dealbreaker rule for critical categories), revises on failure, and loops until it passes or `maxRounds` is hit. Full rubric lives in the script's `RUBRIC` constant.
  - **Prerequisites on the machine running it:** Codex CLI (`codex`, logged in) and Antigravity CLI (`agy`, logged in) both on PATH. The script currently hardcodes the Antigravity binary at `/Users/edge_ai/.local/bin/agy` — adjust that path if installed elsewhere.
  - Usage: `Workflow({scriptPath: "workflows/verify-task.js", args: {task, result, persona, cwd, maxRounds}})`, or copy/symlink it into `~/.claude/workflows/` to invoke by `name: "verify-task"`.

Update this file as more skills/workflows are added.
