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
`claude -p` yet (that's Phase 3) — a bare "!command" prefix keeps the
surface small and auditable. Phase 2 v1 (weekly-report.sh) and Phase 2.5
(work-log-stop-check.sh) both handle retries; verify-task-v2 clarification
retry remains unimplemented (see docs/discord-bot.md) since it needs a
resume mechanism that doesn't exist yet, unlike the other two which are
just re-running a self-contained script.
"""
import asyncio
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import discord

CONFIG_PATH = Path.home() / ".claude" / "discord-bot" / "config.json"
MAC_AGENT = Path.home() / "mac-agent"
WEEKLY_REPORT_SH = MAC_AGENT / "cron" / "weekly-report.sh"
WORK_LOG_STOP_CHECK_SH = MAC_AGENT / "hooks" / "work-log-stop-check.sh"
STATE_DIR = Path.home() / ".claude" / "hooks-state"
WORK_LOG_DISPATCHED_MARKER_DIR = STATE_DIR / "work-log"

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

    # Unhandled type (e.g. a future Phase 2.5 source) — log and ignore rather
    # than silently pretending we did something.
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
    # Phase 1: anything else is silently ignored (no free-chat relay yet).


if __name__ == "__main__":
    client.run(CONFIG["token"])
