# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Source of truth for Claude Code assets (skills, workflows) shared across a fleet of agents. Each agent pulls this repo in (symlink into `~/.claude/` for personal use, or via a plugin marketplace manifest for shared use — marketplace setup is still pending).

## 세션 운영 규율

- **세션 중간에 CLAUDE.md를 고치거나 모델을 바꾸지 말 것.** 둘 다 프롬프트 캐시 프리픽스를
  바꿔서 그 시점 이후 전체가 캐시 미스로 재계산된다(캐시 TTL 5분 — 프리픽스가 달라지면
  이전 캐시는 재사용 불가). 수정·전환은 항상 세션 경계(새 세션 시작 시)에서만.
  (출처: 실밸개발자 Claude Code 강의 2편, 2026-07-29 검토·반영)

## Structure

This is an index only — each entry is a one/two-line pointer. Full rationale, known bugs, and porting notes live in `docs/*.md`. Read the linked doc before touching that script; don't assume the one-liner here is the whole story.

## Contents

- `setup.sh` — run this first on any new agent/machine. Checks Codex CLI and Antigravity CLI (`agy`), installs via `brew install --cask` if missing, then guides login (OAuth can't be done headlessly — a human has to approve in a browser).

- `workflows/verify-task.js` — independent-verification Workflow: scores a completed task with both Codex and Antigravity/Gemini against a shared rubric, revises on failure, loops until it passes. → [docs/verify-task.md](docs/verify-task.md)
- `workflows/verify-task-v2.js` — tiered (light/full) multi-agent review. Light track unchanged (Claude executes, Codex scores vs 90pt). Full track (2026-07-27 redesign, no rubric): Codex authors its own plan → Claude+Antigravity critique it blindly → Codex reconciles/revises → executes → Claude+Antigravity dual no-score code review gates completion. Findings accumulate permanently into `docs/codex-harness.md`, which Codex is forced to read before every plan/execute call. → [docs/verify-task-v2-design.md](docs/verify-task-v2-design.md)
- `hooks/verify-task-stop-check.sh` — Stop hook, two tiers: MANDATORY (no escape valve) if 코딩(3+ Edit/Write) or 아이디어 회의(`ExitPlanMode` used) happened without a `verify-task` run; SOFT (escape valve intact) for risky Bash alone. Mandatory-category registry grows one item at a time. → [docs/verify-task-stop-check.md](docs/verify-task-stop-check.md)
- `hooks/idea-meeting-plan-mode.sh` — UserPromptSubmit hook: prompt containing "아이디어 회의" injects a nudge to call `EnterPlanMode`, so the mandatory category above has something to detect. → [docs/idea-meeting-plan-mode.md](docs/idea-meeting-plan-mode.md)
- `workflows/lib/coach-headroom.sh` / `usage-advisor.sh` / `route-dispatch.sh` — usage-balancing routing per Rule A (objectified 2026-07-27: exempt only for 독립검사 실행 중 / 코덱스가 못 쓰는 도구 필요 — no more subjective "orchestrator judgment") / Rule B ("안티그래비티는 명시적 트리거 시만"): codex-capable work compares codex vs Claude headroom (advisor, not fully enforceable — "prefer claude" just means the session proceeds itself), simple work tries antigravity then falls back to codex (dispatch, fully enforced). Not used by verify-task(-v2)'s fixed author/grader role assignments. → [docs/usage-routing.md](docs/usage-routing.md)
- `hooks/usage-routing-check.sh` — Stop hook that nags (once per session) if Claude did substantial direct work while Claude's own `coach` usage level was yellow/red with no sign of consulting the routing policy, unless a Rule A exception (독립검사 스킬 실행 / `mcp__claude-in-chrome__*` 사용) is present. → [docs/usage-routing.md](docs/usage-routing.md)
- `hooks/session-cost-gate-stop-check.sh` — Stop hook that nags (once per session) when the most recent turn's context size crosses 180K tokens, suggesting a new session. → [docs/session-cost-gate.md](docs/session-cost-gate.md)

### 일정비서 (work/calendar secretary)

Three pieces that together turn finished work (Claude Code sessions, shared recordings, shared documents) into an archived record + calendar entry, plus a Friday rollup:

- `hooks/work-log-stop-check.sh` — Stop hook: classifies each session as public-work vs. meta/private, archives output files + logs a Calendar event for public-work sessions. → [docs/worklog-hook.md](docs/worklog-hook.md)
- `cron/weekly-report.sh` — Thursday 18:00 `launchd` job: compiles the week's Calendar entries into a "이번 주 한 일 / 다음 주 할 일" report, saved to Drive + logged as a Friday Calendar event. Headless `claude -p` invocations hang intermittently under launchd; mitigated with a watchdog + 3x retry. → [docs/weekly-report.md](docs/weekly-report.md)
- `bin/transcribe.sh` — local, offline Whisper transcription for shared audio recordings; feeds into the same archive+calendar flow as documents. → [docs/transcribe.md](docs/transcribe.md)
- `bin/discord-bot.py` — persistent (launchd `KeepAlive`) Discord bot: `!주간보고서` / `!상태` on-demand triggers, channel-scoped. `bin/discord-notify.sh` — one-way fire-and-forget notify helper (used by weekly-report.sh, work-log-stop-check.sh, verify-task-v2.js escalations), works independently of the bot process. → [docs/discord-bot.md](docs/discord-bot.md)
- `cron/kakao-morning-briefing.sh` — daily 09:00 `launchd` job: sends today's Calendar events + weather + a 3-topic news digest via KakaoTalk "나에게 보내기", reached through Kakao's Play MCP gateway. Connection is via `mcporter` (a generic MCP bridge tool, not Kakao-specific) registered into Claude Code as a user-scope stdio MCP server — Claude Code's own native remote-MCP OAuth was tried first and rejected by PlayMCP's server (IP-allowlist error), so this is the working fallback. → [docs/kakao-playmcp.md](docs/kakao-playmcp.md)
- `cron/token-cost-report.sh` — daily 23:00 `launchd` job: runs the Drive-installed `token-cost-dashboard` portable skill's `analyze_sessions.py` against the repo set defined in `config/tracked-repos.json` (single source of truth, 2026-07-29 — add a repo there, not by editing this script), saves dated HTML reports to Drive. Pure python3 (no `claude -p`), so no watchdog/retry needed. Sibling skill `ai-readiness-cartography` deliberately NOT cron'd (expensive Claude-judgment run, codebase structure doesn't change daily enough to justify it) — first real run's dashboard is at `Drive/AI준비도리포트/2026-07-29/mac-agent.html`. → [docs/token-cost-report.md](docs/token-cost-report.md)

Also relevant: the Google Drive MCP connector, previously misauthenticated as a third party's account, was reconnected to `sungtac@gmail.com` and its `permissions.deny` block removed (2026-07-28, verified against `~/.claude/settings.json`). `weekly-report.sh` still deliberately avoids Drive MCP by design (uses local filesystem instead) — that's an independent decision, not a workaround for the old account issue. Details in [docs/weekly-report.md](docs/weekly-report.md).

Update this file (and the matching `docs/*.md`) as more skills/workflows are added. Keep entries here to 1-2 lines — push narrative detail into the linked doc.
