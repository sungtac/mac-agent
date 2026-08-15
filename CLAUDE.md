# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Source of truth for Claude Code assets (skills, workflows) shared across a fleet of agents. Each agent pulls this repo in (symlink into `~/.claude/` for personal use, or via a plugin marketplace manifest for shared use — marketplace setup is still pending).

## 세션 운영 규율

- **세션 중간에 CLAUDE.md를 고치거나 모델을 바꾸지 말 것.** 둘 다 프롬프트 캐시 프리픽스를
  바꿔서 그 시점 이후 전체가 캐시 미스로 재계산된다(캐시 TTL 5분 — 프리픽스가 달라지면
  이전 캐시는 재사용 불가). 수정·전환은 항상 세션 경계(새 세션 시작 시)에서만.
  (출처: 실밸개발자 Claude Code 강의 2편, 2026-07-29 검토·반영)
- **컨텍스트가 30~40만 토큰에 이르기 전에 자동 `/compact`를 기다리지 말고 수동으로 선제 실행할 것.** compact 지시 때는 목표/결정사항, 제약조건, 변경된 파일 경로, 발견한 버그를 반드시 보존하도록 명시적으로 지시하라. 이는 같은 작업을 다음 창으로 이어갈 때만 쓰며, 완전히 새 작업으로 전환할 때는 `/clear`를 사용하고, 그 전에 상태를 남기려면 `/save-state` 스킬(`~/.claude/skills/save-state/SKILL.md` / `restore-state/SKILL.md`)을 대신 사용하라. (출처: 유튜브 컨텍스트 관리 영상 + Antigravity 리서치 재검증, 2026-07-31)

## 공통 에이전트 답변 규칙

- Claude·Codex·Antigravity의 기준 아이덴티티와 persona는 `config/agent-profile-contract.json`에 있다. 자세한 운영 설명은 `docs/agent-identity-and-persona.md`를 읽어라.
- 사용자에게 답할 때는 일반인이 이해할 수 있는 고등학생 수준으로 설명하고, 결론을 먼저 말하라.
- 어려운 용어는 처음 나올 때 쉽게 풀어 쓰고, 실제로 하지 않은 작업을 완료했다고 말하지 마라.
- 사용자용 답변에는 장식용 `###`와 `**` 문법을 사용하지 마라.

## Structure

This is an index only — each entry is a one/two-line pointer. Full rationale, known bugs, and porting notes live in `docs/*.md`. Read the linked doc before touching that script; don't assume the one-liner here is the whole story.

## Contents

- `setup.sh` — run this first on any new agent/machine. Checks Codex CLI and Antigravity CLI (`agy`), installs via `brew install --cask` if missing, then guides login (OAuth can't be done headlessly — a human has to approve in a browser).

- `bin/verify-task-orchestrator.py` — the only verification entrypoint: deterministic file handoff plus light/full subscription-CLI review. The removed JS Workflow adapter must not be reintroduced; the harness lives in `bin/verify-task-harness.py`. → [docs/verify-task-v2-design.md](docs/verify-task-v2-design.md)
- `bin/edge_agent_improvement.py` — every failed/blocked harness or provider-pilot result becomes an idempotent queued improvement task; no blocker-only dead-end handoff. → [docs/edge-agent-improvement-contract.md](docs/edge-agent-improvement-contract.md)
- `bin/edge_agent_absence_guard.py` + `bin/edge_agent_capability_preflight.py` — bounded source discovery and a hard gate against unsupported “missing/unavailable/not configured” claims. Secret values are never read into evidence.
- `bin/edge_agent_parallel_audit.py` — a human-run, read-only diagnostic CLI for checking worktrees, manifests, and reservations when manual inspection is needed.
- `bin/edge_agent_provider_pilot.py` — a human-run operations CLI for piloting one approved provider in a clean worktree; `--execute` requires `--confirm-live-provider`.
- `bin/edge_agent_auth_boundary.py` — a human-run, read-only credential audit and migration-planning helper that never reads credential file contents.
- `bin/edge_agent_completion_harness.py` — a human-run, multi-domain completion gate for the “completed multi-agent operating system” goal, with `init`, `check-services`, `check-repos`, `check-command`, `check-canary`, `status`, and `complete` subcommands. → [docs/edge-agent-completion-harness.md](docs/edge-agent-completion-harness.md)
- `hooks/verify-task-stop-check.sh` — Stop hook, two tiers: MANDATORY (no escape valve) if 코딩(3+ Edit/Write) or 아이디어 회의(`ExitPlanMode` used) happened without a `verify-task-v2` run; SOFT (escape valve intact) for risky Bash alone. Mandatory-category registry grows one item at a time. → [docs/verify-task-stop-check.md](docs/verify-task-stop-check.md)
- `hooks/idea-meeting-plan-mode.sh` — UserPromptSubmit hook: prompt containing "아이디어 회의" injects a nudge to call `EnterPlanMode`, so the mandatory category above has something to detect. → [docs/idea-meeting-plan-mode.md](docs/idea-meeting-plan-mode.md)
- `workflows/lib/coach-headroom.sh` / `usage-advisor.sh` / `route-dispatch.sh` — usage-balancing routing per Rule A (objectified 2026-07-27: exempt only for 독립검사 실행 중 / 코덱스가 못 쓰는 도구 필요 — no more subjective "orchestrator judgment") / Rule B ("안티그래비티는 명시적 트리거 시만"): codex-capable work compares codex vs Claude headroom (advisor, not fully enforceable — "prefer claude" just means the session proceeds itself), simple work tries antigravity then falls back to codex (dispatch, fully enforced). Not used by `verify-task-v2`'s fixed author/reviewer role assignments. → [docs/usage-routing.md](docs/usage-routing.md)
- `hooks/usage-routing-check.sh` — Stop hook that nags (once per session) if Claude did substantial direct work while Claude's own `coach` usage level was yellow/red with no sign of consulting the routing policy, unless a Rule A exception (독립검사 스킬 실행 / `mcp__claude-in-chrome__*` 사용) is present. → [docs/usage-routing.md](docs/usage-routing.md)
- `hooks/session-cost-gate-stop-check.sh` — Stop hook that nags (once per session) when the most recent turn's context size crosses 180K tokens, suggesting a new session. → [docs/session-cost-gate.md](docs/session-cost-gate.md)

### 일정비서 (work/calendar secretary)

Three pieces that together turn finished work (Claude Code sessions, shared recordings, shared documents) into an archived record + calendar entry, plus a Friday rollup:

- `hooks/work-log-stop-check.sh` — Stop hook: classifies each session as public-work vs. meta/private, archives output files + logs a Calendar event for public-work sessions. → [docs/worklog-hook.md](docs/worklog-hook.md)
- `cron/weekly-report.sh` — Thursday 18:00 `launchd` job: compiles the week's Calendar entries into a "이번 주 한 일 / 다음 주 할 일" report, saved to Drive + logged as a Friday Calendar event. Headless `claude -p` invocations hang intermittently under launchd; mitigated with a watchdog + 3x retry. → [docs/weekly-report.md](docs/weekly-report.md)
- `bin/transcribe.sh` — local, offline Whisper transcription for shared audio recordings; feeds into the same archive+calendar flow as documents. → [docs/transcribe.md](docs/transcribe.md)
- Discord integration (`bin/discord-bot.py`, `bin/codex-bot.py`, `bin/discord_bot_common.py`, `bin/discord-notify.sh`) is retired. Source, tests, and pending state remain preserved for quarantine/rollback only; it is not an active runtime or routing path. → [docs/discord-bot.md](docs/discord-bot.md)
- `cron/kakao-morning-briefing.sh` — daily 09:00 `launchd` job: sends today's Calendar events + weather + a 3-topic news digest via KakaoTalk "나에게 보내기", reached through Kakao's Play MCP gateway. Connection is via `mcporter` (a generic MCP bridge tool, not Kakao-specific) registered into Claude Code as a user-scope stdio MCP server — Claude Code's own native remote-MCP OAuth was tried first and rejected by PlayMCP's server (IP-allowlist error), so this is the working fallback. → [docs/kakao-playmcp.md](docs/kakao-playmcp.md)
- `cron/token-cost-report.sh` — daily 23:00 `launchd` job: runs the Drive-installed `token-cost-dashboard` portable skill's `analyze_sessions.py` against the repo set defined in `config/tracked-repos.json` (single source of truth, 2026-07-29 — add a repo there, not by editing this script), saves dated HTML reports to Drive. Pure python3 (no `claude -p`), so no watchdog/retry needed. Sibling skill `ai-readiness-cartography` deliberately NOT cron'd (expensive Claude-judgment run, codebase structure doesn't change daily enough to justify it) — first real run's dashboard is at `Drive/AI준비도리포트/2026-07-29/mac-agent.html`. → [docs/token-cost-report.md](docs/token-cost-report.md)

Also relevant: the Google Drive MCP connector, previously misauthenticated as a third party's account, was reconnected to `sungtac@gmail.com` and its `permissions.deny` block removed (2026-07-28, verified against `~/.claude/settings.json`). `weekly-report.sh` still deliberately avoids Drive MCP by design (uses local filesystem instead) — that's an independent decision, not a workaround for the old account issue. Details in [docs/weekly-report.md](docs/weekly-report.md).

Update this file (and the matching `docs/*.md`) as more skills/workflows are added. Keep entries here to 1-2 lines — push narrative detail into the linked doc.
