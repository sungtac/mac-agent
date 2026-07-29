#!/usr/bin/env python3
"""Shared helpers between discord-bot.py (맥, Claude-side: 주간보고서/상태/
자유채팅) and codex-bot.py (코덱스 전용, 2026-07-29) — split into its own
module rather than duplicated across both files so a fix to the usage gate,
the graceful-kill escalation, or SUBPROCESS_ENV's PATH handling lands once
for both bots instead of risking the two copies silently drifting apart.

Both bot scripts run as independent launchd-managed processes with their own
Discord token/config — this module has no discord.py dependency of its own
and no side effects at import time, so either bot can import from it freely.
"""
import asyncio
import os
import signal
from pathlib import Path

MAC_AGENT = Path.home() / "mac-agent"

# Natural-chat wake words for codex-bot.py's handle_codex_chat_wake — a
# message starting with one of these (e.g. "코덱스야 ...", "콕스 ...") is
# addressed to Codex, not Claude (2026-07-29, user's explicit request to
# address bots by name like ChatGPT rather than typing a command every
# turn). Lives here, not in codex-bot.py, because discord-bot.py's own
# free-chat catch-all must exclude the SAME set of words — both bots sit in
# the same channel and see every message, so if this list ever drifted
# between the two files, a wake-worded message could get answered by BOTH
# bots (or by neither).
CODEX_CHAT_WAKE_WORDS = ("코덱스", "콕스")

# Subprocesses we spawn shell out to codex/agy/claude by absolute path
# already, but git/date/etc still resolve via PATH — launchd's own PATH for
# this process can be the stripped /usr/bin:/bin:/usr/sbin:/sbin default, so
# give spawned children a real one explicitly rather than rediscovering this
# gotcha yet again (same recurring issue as codex/agy/ffmpeg/whisper-cli/tmux/
# coach elsewhere in this repo, see docs/discord-bot.md).
SUBPROCESS_ENV = {
    **os.environ,
    "PATH": f"/opt/homebrew/bin:{Path.home()}/.local/bin:" + os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
}

USAGE_PREFLIGHT_GATE_SH = MAC_AGENT / "workflows" / "lib" / "usage-preflight-gate.sh"
USAGE_GATE_TIMEOUT_SECONDS = 15  # should return in well under a second normally; bounds a `coach` hang instead of wedging the caller's lock forever


async def usage_gate_check(actor: str) -> str | None:
    """Runs usage-preflight-gate.sh <actor> and returns the human-readable
    skip reason (SKIP:'s text with that prefix stripped) if usage is too low
    to safely start, or None if it's fine to proceed.

    Callers are already live replies to a real message.channel, so on SKIP
    they just tell the user directly and return — no pending-job/auto-retry
    needed here, the user can just re-send the command once usage recovers.

    Fails open (returns None, i.e. "proceed") on any subprocess error OR
    timeout — a broken gate must not become a new way for these commands to
    stop working, same posture as every bash call site's
    `|| echo "PROCEED..."`.

    15s timeout: callers hold a lock (FREE_CHAT_LOCK / CODEX_DISPATCH_LOCKS)
    around this call, so an unbounded hang here doesn't just delay one
    message — it permanently wedges that lock. `coach` (the underlying data
    source, via usage-preflight-gate.sh) has been observed mid-session to
    report a query-timeout condition for one of its own provider checks,
    confirming it can genuinely stall — this guards against that propagating
    upward into an unrecoverable lock.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", str(USAGE_PREFLIGHT_GATE_SH), actor,
            env=SUBPROCESS_ENV,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=USAGE_GATE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            return None
        text = out.decode(errors="replace").strip()
    except Exception:
        return None
    if text.startswith("SKIP:"):
        return text[len("SKIP:"):].strip()
    return None


def _kill_process_group(proc) -> None:
    """`proc.kill()` only SIGKILLs that one direct child — if it spawned its
    own children (e.g. `claude -p`/`codex exec` running a shell tool call),
    those become orphans that keep running after "kill" (confirmed: a
    `sleep 60 & wait` child under a plain `create_subprocess_exec`d bash
    survived `proc.kill()` with the sleep still alive; the same repro under
    `start_new_session=True` + `os.killpg` left nothing behind). Requires the
    process to have been spawned with `start_new_session=True` so it's its
    own process-group leader — falls back to a plain `proc.kill()` if the
    group lookup fails (process already gone, or wasn't a group leader).
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


async def _kill_process_group_graceful(proc, grace_seconds: float = 2.0) -> None:
    """SIGTERM first, escalate to _kill_process_group's SIGKILL only if the
    process group hasn't exited within `grace_seconds`. A bare SIGKILL
    mid-write (e.g. `codex exec -s workspace-write` or `claude -p` with full
    tool access, both mid-timeout-kill) risks leaving a partially-written
    file behind right when the caller's own before/after diff most needs a
    coherent post-kill state to compare against — same reasoning
    weekly-report.sh already applies to its own claude -p timeouts.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        _kill_process_group(proc)
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        _kill_process_group(proc)
