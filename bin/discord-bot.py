#!/usr/bin/env python3
"""Discord bot — Phase 1 (on-demand automation triggers) + Phase 2 v1
(reply-triggered retry for weekly-report.sh) + Phase 2.5 (reply-triggered
retry for work-log-stop-check.sh; see handle_pending_reply).
Runs as a persistent process under launchd (KeepAlive) since it holds a
Gateway WebSocket connection — this is NOT a periodic cron job like
weekly-report.sh.

Config: ~/.claude/discord-bot/config.json — {"token": "...", "channel_id": "..."}
Not committed to this (public) repo; lives outside it entirely.

Authorization: only messages posted in the configured channel_id are acted
on. Everything else (other channels, DMs, other servers, the bot's own
messages) is ignored — this is the whole trust boundary, since the channel
is invite-only and everyone in it is treated as fully trusted (the user's
own explicit decision, not a default to weaken later without re-deciding).
Reply-triggered retries inherit this same boundary (checked before dispatch).

Phase 1 scope: two deterministic commands only. No free-form chat relay to
`claude -p` (that's still unimplemented, "Phase 3") — a bare "!command"
prefix keeps the surface small and auditable. Phase 2 v1 (weekly-report.sh)
and Phase 2.5 (work-log-stop-check.sh) both handle retries; verify-task-v2
clarification retry remains unimplemented (see docs/discord-bot.md) since it
needs a resume mechanism that doesn't exist yet, unlike the other two which
are just re-running a self-contained script.

`!코덱스 <repo-alias> <task>` (2026-07-28): dispatches a real, write-capable
Codex run via workflows/lib/codex-execute-dispatch.sh, restricted to
CODEX_REPO_ALIASES and gated by FREE_CHAT_USER_ID — this is NOT covered by
the channel-wide trust boundary above (see handle_codex_dispatch).
"""
import asyncio
import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import discord

CONFIG_PATH = Path.home() / ".claude" / "discord-bot" / "config.json"
MAC_AGENT = Path.home() / "mac-agent"
WEEKLY_REPORT_SH = MAC_AGENT / "cron" / "weekly-report.sh"
WORK_LOG_STOP_CHECK_SH = MAC_AGENT / "hooks" / "work-log-stop-check.sh"
CODEX_EXECUTE_DISPATCH_SH = MAC_AGENT / "workflows" / "lib" / "codex-execute-dispatch.sh"
VERIFY_TASK_V2_JS = MAC_AGENT / "workflows" / "verify-task-v2.js"
CLAUDE_BIN = Path.home() / ".local" / "bin" / "claude"
VERIFY_TASK_V2_RETRY_TIMEOUT_SECONDS = 30 * 60  # full-track verify-task-v2 round-trips codex/antigravity several times, same budget as !코덱스
STATE_DIR = Path.home() / ".claude" / "hooks-state"
WORK_LOG_DISPATCHED_MARKER_DIR = STATE_DIR / "work-log"

# !코덱스 target allowlist — never let a Discord message pick an arbitrary
# absolute path for `codex exec -s workspace-write`, which really writes
# files. Add a line here (after confirming with the user) to expose another
# repo; do not accept a free-typed path from the message itself.
CODEX_REPO_ALIASES = {
    "mac-agent": MAC_AGENT,
    "hwpx-skill": Path.home() / "document-writing-project" / "hwpx-skill",
    "pptx-skill": Path.home() / "document-writing-project" / "pptx-skill",
}
CODEX_DISPATCH_TIMEOUT_SECONDS = 30 * 60  # coding tasks can run longer than a report generation

# Phase 2: pending-job store for reply-triggered retries. Written by scripts
# that escalate via discord-notify.sh (keyed by that call's returned message
# id), read here when a reply comes in. Schema: {"type": ..., "created_at":
# ISO string, "params": {...}}. `type` is dispatched below; unrecognized
# types (future Phase 2.5 sources) are logged and ignored rather than erroring,
# so this store can grow without breaking older/newer bot code.
PENDING_DIR = Path.home() / ".claude" / "discord-bot" / "pending"
PENDING_MAX_AGE_HOURS = 48

# Subprocesses we spawn (weekly-report.sh etc.) shell out to codex/agy/claude
# by absolute path already (fixed 2026-07-26), but git/date/etc still resolve
# via PATH — launchd's own PATH for this process can be the stripped
# /usr/bin:/bin:/usr/sbin:/sbin default, so give spawned children a real one
# explicitly rather than rediscovering this gotcha yet again.
SUBPROCESS_ENV = {
    **os.environ,
    "PATH": f"/opt/homebrew/bin:{Path.home()}/.local/bin:" + os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
}

WEEKLY_REPORT_TIMEOUT_SECONDS = 20 * 60  # generous margin above the script's own ~13min worst case (3 attempts x 240s + pauses)


def load_config():
    if not CONFIG_PATH.exists():
        print(f"FATAL: config not found at {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except Exception as e:
        print(f"FATAL: config at {CONFIG_PATH} is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    if not cfg.get("token") or not cfg.get("channel_id"):
        print(f"FATAL: config at {CONFIG_PATH} missing token or channel_id", file=sys.stderr)
        sys.exit(1)
    return cfg


CONFIG = load_config()
AUTHORIZED_CHANNEL_ID = str(CONFIG["channel_id"])
# Channel trust alone is not enough for a command that lets a channel member
# cause arbitrary code execution (`!코덱스`, workspace-write) — this was the
# documented intent for this field since Phase 1 (see docs/discord-bot.md
# "권한 경계"), unused until now. Empty/missing = fail closed (nobody passes).
FREE_CHAT_USER_ID = str(CONFIG.get("free_chat_user_id") or "")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def tail_file(path: Path, n_lines: int = 15) -> str:
    if not path.exists():
        return "(로그 파일 없음)"
    try:
        lines = path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n_lines:]) or "(빈 파일)"
    except Exception as e:
        return f"(읽기 실패: {e})"


async def handle_status(message: discord.Message):
    import datetime

    today = datetime.date.today().isoformat()

    weekly_log = STATE_DIR / "weekly-report" / f"{today}.log"
    worklog_dir = STATE_DIR / "work-log"
    nag_dir = STATE_DIR / "verify-task-nag"

    recent_worklog = "(없음)"
    if worklog_dir.exists():
        logs = sorted(worklog_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
        if logs:
            recent_worklog = "\n".join(f"- {p.stem}: {p.read_text(errors='replace').strip()[:150]}" for p in logs)

    lines = [
        "**일정비서 상태**",
        f"주간보고서 오늘({today}) 로그:\n```\n{tail_file(weekly_log, 10)}\n```",
        f"최근 work-log 처리 3건:\n{recent_worklog}",
    ]
    await message.channel.send("\n".join(lines)[:1900])


async def handle_weekly_report(message: discord.Message):
    await message.channel.send("주간보고서 지금 실행합니다 — 최대 20분 정도 걸릴 수 있어요, 끝나면 알려드릴게요.")
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", str(WEEKLY_REPORT_SH),
            env=SUBPROCESS_ENV,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=WEEKLY_REPORT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await message.channel.send(f"⚠️ 주간보고서 실행이 {WEEKLY_REPORT_TIMEOUT_SECONDS}초를 넘어서 강제 종료했습니다 — 스크립트 자체 watchdog도 못 잡은 이상 상황이라 직접 확인이 필요합니다.")
            return
        tail = "\n".join((stdout or b"").decode(errors="replace").splitlines()[-10:])
        if proc.returncode == 0:
            await message.channel.send(f"✅ 주간보고서 완료.\n```\n{tail}\n```"[:1900])
        elif proc.returncode == 3:
            # weekly-report.sh's own lockfile mutex: another run (launchd's
            # schedule, a manual !주간보고서, or another reply) was already
            # in progress. Not a failure — say so plainly rather than
            # showing a red X for something that didn't actually break.
            await message.channel.send(f"⏳ 이미 다른 실행이 진행 중이라 이번 요청은 건너뛰었습니다. 그 실행이 끝난 뒤 상태를 `!상태`로 확인해주세요.\n```\n{tail}\n```"[:1900])
        else:
            await message.channel.send(f"❌ 주간보고서 실패 (exit={proc.returncode}).\n```\n{tail}\n```"[:1900])
    except Exception as e:
        await message.channel.send(f"❌ 주간보고서 실행 중 예외: {e}")


async def handle_work_log_retry(message: discord.Message, params: dict):
    """Re-run work-log-stop-check.sh for one specific session, triggered by
    a Discord reply. Unlike weekly-report.sh (a self-contained script with
    no external state), this hook is Stop-hook-only by design: it reads
    session_id/transcript_path from stdin JSON that Claude Code itself
    normally supplies. A bare re-invocation (mirroring handle_weekly_report's
    no-stdin subprocess call) would hit the script's own `[ -z "$SESSION_ID"
    ] && exit 0` guard and silently no-op — so this synthesizes that same
    stdin JSON from the pending-job's captured params instead.

    The script also unconditionally touches a `.dispatched` marker on first
    run and never cleans it up (a same-session-once guard against Stop
    firing multiple times via /clear, /compact, /resume) — that marker must
    be removed here first, or this retry would ALSO silently no-op at that
    check, one line before the real work.

    Does NOT await the actual archive+Calendar work: the script backgrounds
    that part internally (`( ... ) & disown`) and returns almost immediately
    by design (so the original Stop hook never blocks session exit) — this
    call only confirms the top-level dispatch itself started cleanly. The
    real success/failure signal arrives later, independently, via the
    script's own Phase 2.5 completion notification (see
    hooks/work-log-stop-check.sh) — not from this function.

    stdout/stderr are DEVNULL, not PIPE, and this awaits `proc.wait()`, not
    `proc.communicate()` — a real bug caught by live testing (2026-07-28):
    the disowned background subshell inherits the script's stdout/stderr
    file descriptors and holds them open for as long as IT runs (up to the
    script's own 300s watchdog), even though the top-level `bash` process
    itself returns in milliseconds. `communicate()` waits for those pipes to
    hit EOF, not just for the process to exit, so it silently blocked for
    the full 30s timeout on every single call in practice — confirmed via a
    live test end-to-end (PIPE reliably timed out at 30s; DEVNULL + wait()
    returned in ~0.02s for the identical dispatch). Do not "fix" this back
    to PIPE to capture output — there is no output worth capturing here
    anyway (nothing meaningful is written to the top-level script's own
    stdout/stderr; the real diagnostics all live in the per-session
    LOGFILE/DEBUG_LOGFILE inside work-log-stop-check.sh).
    """
    session_id = params.get("session_id")
    transcript_path = params.get("transcript_path")
    if not session_id or not transcript_path:
        await message.channel.send(f"❌ work-log 재시도 실패 — pending-job에 session_id/transcript_path가 없습니다: {params!r}")
        return

    marker = WORK_LOG_DISPATCHED_MARKER_DIR / f"{session_id}.dispatched"
    marker.unlink(missing_ok=True)

    stdin_json = json.dumps({"session_id": session_id, "transcript_path": transcript_path}).encode()

    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", str(WORK_LOG_STOP_CHECK_SH),
            env=SUBPROCESS_ENV,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        proc.stdin.write(stdin_json)
        await proc.stdin.drain()
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), timeout=30)
        if proc.returncode == 0:
            await message.channel.send(f"재시도 시작했습니다 (세션 {session_id}) — 완료되면 따로 알려드릴게요.")
        else:
            await message.channel.send(f"❌ work-log 재시도 디스패치 자체가 실패했습니다 (exit={proc.returncode}). 로그: ~/.claude/hooks-state/work-log/{session_id}.log")
    except asyncio.TimeoutError:
        proc.kill()
        await message.channel.send(f"⚠️ work-log 재시도 디스패치가 30초 안에 반환되지 않았습니다 — 정상이라면 거의 즉시 반환돼야 하는데 이상 상황입니다. 세션 {session_id} 직접 확인이 필요합니다.")
    except Exception as e:
        await message.channel.send(f"❌ work-log 재시도 실행 중 예외: {e}")


async def handle_verify_task_v2_retry(message: discord.Message, params: dict):
    """Re-run verify-task-v2.js's full pipeline from scratch with the user's
    Discord reply appended as the answer to a needs_clarification question.

    verify-task-v2.js has no internal resume/checkpoint mechanism at all (see
    docs/verify-task-v2-design.md and the workflow's own needs_clarification
    comment) — the only documented recovery path is "answer the question,
    append it to the original task string, and re-invoke the whole workflow
    from the top." There is also no bash-script entry point the way
    weekly-report.sh/work-log-stop-check.sh have — verify-task-v2.js can only
    be invoked through Claude Code's own `Workflow` tool, which nothing in
    this file had ever called before (weekly-report.sh/work-log-stop-check.sh
    are bash scripts spawned directly; !코덱스 calls `codex exec` directly).
    So this spawns a headless `claude -p` and instructs it, in natural
    language, to call `Workflow({scriptPath: ..., args: {...}})` itself —
    confirmed working via a live probe workflow before this was built
    (2026-07-28): a trivial `probe.js` invoked this way returned its result
    correctly through headless `claude -p`.

    Unlike handle_work_log_retry, this DOES await the full run to completion
    (stdout=PIPE + communicate(), not DEVNULL + wait()) — verify-task-v2.js
    runs synchronously to completion inside the `claude -p` process itself,
    it does not background+disown the way work-log-stop-check.sh does, so
    there is no inherited-pipe-stays-open hazard here (see the docstring on
    handle_work_log_retry for that bug and why it doesn't apply to this
    function).

    If this retry run itself hits needs_clarification again, that is not
    handled specially here — the re-invoked verify-task-v2.js's own
    notifyDiscordEscalation() fires again independently and writes a fresh
    pending-job, so a reply chain (answer -> still unclear -> answer again)
    works naturally without any extra code in this function.
    """
    task = params.get("task")
    cwd = params.get("cwd")
    if not task or not cwd:
        await message.channel.send(f"❌ verify-task-v2 재시도 실패 — pending-job에 task/cwd가 없습니다: {params!r}")
        return

    answer = message.content.strip()
    if not answer:
        await message.channel.send("❌ 답장 내용이 비어있어서 재시도할 수 없습니다 — 답변 내용을 담아서 다시 답장해주세요.")
        return

    new_task = f"{task}\n\n[사용자 답변]\n{answer}"
    workflow_args = {
        "task": new_task,
        "cwd": cwd,
        "persona": params.get("persona", "일반 사용자"),
        "maxRounds": params.get("maxRounds", 2),
        "historyFile": params.get("historyFile"),
        "harnessFile": params.get("harnessFile"),
    }
    workflow_args = {k: v for k, v in workflow_args.items() if v is not None}

    await message.channel.send("답장 확인 — verify-task-v2를 답변 반영해서 처음부터 재실행합니다. 전체 트랙이면 코덱스/안티그래비티를 여러 번 오가서 몇 분 걸릴 수 있어요, 끝나면 알려드릴게요.")

    prompt = (
        "Workflow 툴을 사용해서 다음을 실행해줘: "
        f"scriptPath는 {json.dumps(str(VERIFY_TASK_V2_JS))}, "
        f"args는 {json.dumps(workflow_args, ensure_ascii=False)}. "
        "실행이 끝나면 반환된 finalVerdict를 요약해서 한국어로 짧게 알려줘 "
        "(통과 여부, needsUserDecision/needs_clarification로 또 끝났으면 그것도 명시)."
    )

    # CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0: a real bug caught by live testing
    # (2026-07-28), not something documented anywhere beforehand. A Workflow
    # call with real agent() calls (unlike the trivial no-agent probe.js used
    # to validate feasibility) runs as an async background task inside
    # `claude -p`'s own runtime, and by default `claude -p` gives up waiting
    # for it after ~600s, prints a "still running in background, I'll notify
    # you" message, and exits with code 0 — but a one-shot `-p` process has
    # nowhere to deliver that later notification, so the workflow run is
    # effectively orphaned and never actually reported (confirmed: verify-task-v2's
    # own historyFile only had the FIRST run's entry, never the retry's, after
    # this happened in a live test). Setting the ceiling to 0 disables that
    # internal give-up-and-detach behavior so `communicate()` genuinely blocks
    # until the workflow finishes — confirmed via a live agent()-calling probe
    # workflow (not the trivial one) returning correctly with this var set.
    verify_task_v2_env = {**SUBPROCESS_ENV, "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"}

    try:
        proc = await asyncio.create_subprocess_exec(
            str(CLAUDE_BIN), "-p", prompt, "--output-format", "text",
            env=verify_task_v2_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=VERIFY_TASK_V2_RETRY_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await message.channel.send(f"⚠️ verify-task-v2 재시도가 {VERIFY_TASK_V2_RETRY_TIMEOUT_SECONDS}초를 넘어서 강제 종료했습니다 — 직접 확인이 필요합니다.")
            return
        tail = "\n".join((stdout or b"").decode(errors="replace").splitlines()[-20:])
        if proc.returncode == 0:
            await message.channel.send(f"✅ verify-task-v2 재시도 완료.\n```\n{tail}\n```"[:1900])
        else:
            await message.channel.send(f"❌ verify-task-v2 재시도 실패 (exit={proc.returncode}).\n```\n{tail}\n```"[:1900])
    except Exception as e:
        await message.channel.send(f"❌ verify-task-v2 재시도 실행 중 예외: {e}")


RETRY_INTENT_KEYWORDS = ("재시도", "retry", "다시")

# Bare substring matching on RETRY_INTENT_KEYWORDS has a real false-positive
# class (caught in code review, 2026-07-28, not live-hit yet): a reply like
# "재시도 필요 없어" or "no need to retry" contains the keyword but means the
# opposite. These markers are deliberately multi-character/specific — NOT
# bare "안"/"않" (Korean negation prefixes), since those are single common
# syllables that show up inside unrelated words (e.g. "안녕") and would
# suppress nearly every retry-intent reply if used here. Fails closed: any
# marker match here means "take no automated action", same as the existing
# no-keyword-found path — an unwanted retry (burns quota/time on a live
# workspace-write run) is worse than a missed one (user can just reply again).
RETRY_NEGATION_MARKERS = (
    "필요없", "필요 없", "하지마", "하지 마", "하지말", "안 해도", "안해도",
    "그만", "말고", "말자",
    "no need", "don't", "dont ", "not necessary", "never mind", "nevermind",
)


def _has_retry_intent(reply_text: str) -> bool:
    lowered = reply_text.lower()
    if not any(kw in lowered for kw in RETRY_INTENT_KEYWORDS):
        return False
    if any(neg in lowered for neg in RETRY_NEGATION_MARKERS):
        return False
    return True


async def handle_verify_task_v2_decision_retry(message: discord.Message, params: dict):
    """Reply-triggered retry for verify-task-v2.js's needsUserDecision
    (max-rounds exhausted with a real accept/retry/manual-intervention
    three-way choice) — as opposed to handle_verify_task_v2_retry, which is
    for needs_clarification (info-gathering re-questions).

    A free-text Discord reply can't cleanly carry a three-way choice, so this
    only acts on one signal: does the reply contain a retry-intent keyword
    (재시도/retry/다시)? If yes, re-run the SAME original task from scratch
    with maxRounds bumped — unlike the clarification retry, nothing is
    appended to the task text, since there's no question being answered.
    If no keyword is found (including "수용"/"수동으로 할게" style replies,
    or anything else), this intentionally takes no automated action — that
    covers both "accept as-is" and "I'll handle it manually" without trying
    to tell them apart, since both mean "don't touch it again automatically."
    """
    task = params.get("task")
    cwd = params.get("cwd")
    if not task or not cwd:
        await message.channel.send(f"❌ verify-task-v2 재시도 실패 — pending-job에 task/cwd가 없습니다: {params!r}")
        return

    reply_text = message.content.strip()
    if not _has_retry_intent(reply_text):
        await message.channel.send("확인했습니다 — 재시도 키워드(재시도/retry/다시)가 없어서 자동 조치 없이 종료합니다.")
        return

    bumped_max_rounds = params.get("maxRounds", 2) + 2
    workflow_args = {
        "task": task,
        "cwd": cwd,
        "persona": params.get("persona", "일반 사용자"),
        "maxRounds": bumped_max_rounds,
        "historyFile": params.get("historyFile"),
        "harnessFile": params.get("harnessFile"),
    }
    workflow_args = {k: v for k, v in workflow_args.items() if v is not None}

    await message.channel.send(f"답장 확인 — verify-task-v2를 maxRounds={bumped_max_rounds}로 늘려 같은 작업을 처음부터 재실행합니다. 전체 트랙이면 코덱스/안티그래비티를 여러 번 오가서 몇 분 걸릴 수 있어요, 끝나면 알려드릴게요.")

    prompt = (
        "Workflow 툴을 사용해서 다음을 실행해줘: "
        f"scriptPath는 {json.dumps(str(VERIFY_TASK_V2_JS))}, "
        f"args는 {json.dumps(workflow_args, ensure_ascii=False)}. "
        "실행이 끝나면 반환된 finalVerdict를 요약해서 한국어로 짧게 알려줘 "
        "(통과 여부, needsUserDecision/needs_clarification로 또 끝났으면 그것도 명시)."
    )

    # Same rationale as handle_verify_task_v2_retry: this awaits the full run
    # to completion (stdout=PIPE + communicate(), CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0)
    # rather than backgrounding — see that function's docstring for why a
    # short give-up-and-detach default silently turns into a false success.
    verify_task_v2_env = {**SUBPROCESS_ENV, "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"}

    try:
        proc = await asyncio.create_subprocess_exec(
            str(CLAUDE_BIN), "-p", prompt, "--output-format", "text",
            env=verify_task_v2_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=VERIFY_TASK_V2_RETRY_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await message.channel.send(f"⚠️ verify-task-v2 재시도가 {VERIFY_TASK_V2_RETRY_TIMEOUT_SECONDS}초를 넘어서 강제 종료했습니다 — 직접 확인이 필요합니다.")
            return
        tail = "\n".join((stdout or b"").decode(errors="replace").splitlines()[-20:])
        if proc.returncode == 0:
            await message.channel.send(f"✅ verify-task-v2 재시도 완료.\n```\n{tail}\n```"[:1900])
        else:
            await message.channel.send(f"❌ verify-task-v2 재시도 실패 (exit={proc.returncode}).\n```\n{tail}\n```"[:1900])
    except Exception as e:
        await message.channel.send(f"❌ verify-task-v2 재시도 실행 중 예외: {e}")


async def _git_output(cwd: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/git", "-C", str(cwd), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace").strip()


async def _dirty_snapshot(cwd: Path) -> dict:
    """filename -> its current unified-diff text (or "UNTRACKED" for a new
    untracked file), for every file the working tree currently shows as
    changed. Used to compute a before/after delta around a Codex run instead
    of trusting a single post-run `git diff --stat` — this repo can have
    other uncommitted work in flight (another terminal, a concurrent
    session) that has nothing to do with this specific dispatch, and a bare
    post-run diff cannot tell the two apart. Caught live (2026-07-28): a
    `!코덱스` run to add one README line reported 3 unrelated files as
    "changed" that were actually pre-existing/concurrent edits from another
    terminal — this snapshot-delta approach is the fix.
    """
    diff_text = await _git_output(cwd, "diff", "--")
    status_text = await _git_output(cwd, "status", "--porcelain")
    snapshot: dict = {}
    current_file = None
    buf: list = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current_file is not None:
                snapshot[current_file] = "\n".join(buf)
            current_file = line.rsplit(" b/", 1)[-1]
            buf = [line]
        else:
            buf.append(line)
    if current_file is not None:
        snapshot[current_file] = "\n".join(buf)
    for line in status_text.splitlines():
        if line.startswith("?? "):
            snapshot[line[3:]] = "UNTRACKED"
    return snapshot


async def handle_codex_dispatch(message: discord.Message):
    """`!코덱스 <저장소별칭> <작업 지시>` — dispatch a real, write-capable
    Codex run via workflows/lib/codex-execute-dispatch.sh (reused as-is; see
    that script's own header for why it's the write-capable sibling of
    score-dispatch.sh). Restricted to CODEX_REPO_ALIASES (never accept a
    free-typed path — workspace-write really writes files) and to
    FREE_CHAT_USER_ID (channel trust alone is not enough for this).

    Per codex-execute-dispatch.sh's own documented design, Codex's self-report
    is never trusted as a verification signal — this function independently
    diffs the repo and reports THAT, not just Codex's claim, and calls out
    the mismatch explicitly if Codex claims success but nothing actually
    changed. Never commits/pushes — that stays a separate, explicit,
    human-reviewed step.

    Takes a `_dirty_snapshot()` before AND after the run and reports only the
    files that differ between the two snapshots — NOT a plain post-run
    `git diff --stat`. Live test (2026-07-28) caught this the hard way: a
    request to add one README line reported 3 unrelated files as "changed"
    that were pre-existing/concurrent edits from another terminal working in
    the same repo. The before/after delta fixes the common case (dirt that
    already existed when the run started) but cannot fully solve a file
    another process edits DURING this run's window — that's a real residual
    limitation of sharing a working tree with a concurrent editor, not
    something a diff-based check alone can close.
    """
    if str(message.author.id) != FREE_CHAT_USER_ID:
        await message.channel.send("이 명령어는 본인만 사용 가능합니다.")
        return

    parts = message.content.strip().split(maxsplit=2)
    aliases = ", ".join(sorted(CODEX_REPO_ALIASES))
    if len(parts) < 3:
        await message.channel.send(f"사용법: `!코덱스 <저장소별칭> <작업 지시>`\n사용 가능한 별칭: {aliases}")
        return

    _, alias, task = parts
    cwd = CODEX_REPO_ALIASES.get(alias)
    if cwd is None:
        await message.channel.send(f"알 수 없는 저장소 별칭: `{alias}`\n사용 가능한 별칭: {aliases}")
        return

    before = await _dirty_snapshot(cwd)
    dirty_note = "\n⚠️ 이 저장소에 이미 커밋 안 된 변경사항이 있습니다 — 최종 결과는 이번 실행분만 골라 보여드릴게요." if before else ""

    prompt_file = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(task)
            prompt_file = Path(f.name)

        await message.channel.send(f"코덱스에게 지시했습니다 ({alias}, 최대 {CODEX_DISPATCH_TIMEOUT_SECONDS // 60}분 정도 걸릴 수 있어요) — 끝나면 알려드릴게요.{dirty_note}")

        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", str(CODEX_EXECUTE_DISPATCH_SH), str(cwd), str(prompt_file),
            env=SUBPROCESS_ENV,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=CODEX_DISPATCH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await message.channel.send(f"⚠️ 코덱스 실행이 {CODEX_DISPATCH_TIMEOUT_SECONDS // 60}분을 넘어서 강제 종료했습니다 — {alias} 저장소를 직접 확인해주세요.")
            return

        raw = (stdout or b"").decode(errors="replace")
        try:
            result = json.loads(raw)
            ok = bool(result.get("ok"))
            codex_message = str(result.get("message", ""))[:1000]
        except Exception:
            ok = False
            codex_message = f"(codex-execute-dispatch.sh 출력이 JSON이 아님) {raw[:1000]}"

        # Never trust Codex's own report — confirm with a real before/after diff.
        after = await _dirty_snapshot(cwd)
        changed = sorted(f for f in after if after[f] != before.get(f))
        changed_tracked = [f for f in changed if after[f] != "UNTRACKED"]
        changed_untracked = [f for f in changed if after[f] == "UNTRACKED"]

        diff_stat = ""
        if changed_tracked:
            diff_stat = await _git_output(cwd, "diff", "--stat", "--", *changed_tracked)
        if changed_untracked:
            new_files_note = "\n".join(f"신규 파일: {f}" for f in changed_untracked)
            diff_stat = f"{diff_stat}\n{new_files_note}" if diff_stat else new_files_note

        lines = [f"{'✅' if ok else '❌'} 코덱스 작업 {'완료' if ok else '실패'} ({alias})."]
        if diff_stat:
            lines.append(f"이번 실행으로 실제 변경된 파일:\n```\n{diff_stat}\n```")
        elif ok:
            lines.append("⚠️ 코덱스는 완료라고 보고했지만 실제 파일 변경은 없습니다 — 자기 보고를 그대로 믿지 마세요.")
        else:
            lines.append("실제 파일 변경 없음.")
        lines.append(f"코덱스 보고:\n```\n{codex_message}\n```")
        lines.append("커밋·푸시는 하지 않았습니다 — 확인 후 필요하면 직접 요청해주세요.")
        await message.channel.send("\n".join(lines)[:1900])
    except Exception as e:
        await message.channel.send(f"❌ 코덱스 실행 중 예외: {e}")
    finally:
        if prompt_file is not None:
            prompt_file.unlink(missing_ok=True)


async def handle_pending_reply(message: discord.Message) -> bool:
    """If `message` is a reply to a message with a pending-job file, handle
    it and return True. Returns False for anything else (not a reply, or a
    reply to a message with no/expired pending job) so on_message can fall
    through to the normal command checks."""
    ref = message.reference
    if ref is None or ref.message_id is None:
        return False
    job_path = PENDING_DIR / f"{ref.message_id}.json"
    if not job_path.exists():
        return False

    try:
        job = json.loads(job_path.read_text())
        created = datetime.datetime.fromisoformat(job["created_at"])
    except Exception as e:
        print(f"pending job {ref.message_id}: unreadable ({e}), removing", file=sys.stderr)
        job_path.unlink(missing_ok=True)
        return False

    if (datetime.datetime.now() - created).total_seconds() > PENDING_MAX_AGE_HOURS * 3600:
        job_path.unlink(missing_ok=True)
        await message.channel.send("이 요청은 48시간이 지나 만료됐습니다 — 다시 실행하려면 `!주간보고서`를 입력해주세요.")
        return True

    job_type = job.get("type")
    job_path.unlink(missing_ok=True)  # delete first so a second reply can't double-trigger

    if job_type == "weekly-report-retry":
        # The ack is a courtesy, not load-bearing — if sending it hiccups
        # (transient Discord API error), still run the actual retry below.
        # The job file is already deleted at this point, so if we returned
        # here on an ack failure without retrying, the retry would be lost
        # silently with no way for the user to trigger it again via reply.
        try:
            await message.channel.send("답장 확인 — 주간보고서 재시도합니다.")
        except Exception as e:
            print(f"pending job {ref.message_id}: ack send failed ({e}), retrying anyway", file=sys.stderr)
        await handle_weekly_report(message)
        return True

    if job_type == "work-log-retry":
        await handle_work_log_retry(message, job.get("params", {}))
        return True

    if job_type == "verify-task-v2-retry":
        await handle_verify_task_v2_retry(message, job.get("params", {}))
        return True

    if job_type == "verify-task-v2-decision-retry":
        await handle_verify_task_v2_decision_retry(message, job.get("params", {}))
        return True

    # Unhandled type (a future source) — log and ignore rather than silently
    # pretending we did something.
    print(f"pending job {ref.message_id}: unhandled type {job_type!r}", file=sys.stderr)
    return True


@client.event
async def on_ready():
    print(f"logged in as {client.user} (id={client.user.id})")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if str(message.channel.id) != AUTHORIZED_CHANNEL_ID:
        return

    if await handle_pending_reply(message):
        return

    content = message.content.strip()
    if content == "!주간보고서":
        await handle_weekly_report(message)
    elif content == "!상태":
        await handle_status(message)
    elif content.startswith("!코덱스"):
        await handle_codex_dispatch(message)
    # Phase 1: anything else is silently ignored (no free-chat relay yet).


if __name__ == "__main__":
    client.run(CONFIG["token"])
