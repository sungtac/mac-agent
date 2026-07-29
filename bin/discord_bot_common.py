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
import contextlib
import fcntl
import hashlib
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


def _force_kill_pgid(pgid: int) -> None:
    """SIGKILL a process group by an already-captured pgid (not re-derived
    from a proc that may have already exited — `os.getpgid(proc.pid)` raises
    ProcessLookupError once that specific pid is gone, even if other members
    of its former group are still alive under the same pgid number)."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _kill_process_group_graceful(proc, grace_seconds: float = 2.0) -> None:
    """SIGTERM first, escalate to SIGKILL only if the process group hasn't
    exited within `grace_seconds`. A bare SIGKILL mid-write (e.g. `codex exec
    -s workspace-write` or `claude -p` with full tool access, both
    mid-timeout-kill) risks leaving a partially-written file behind right
    when the caller's own before/after diff most needs a coherent post-kill
    state to compare against — same reasoning weekly-report.sh already
    applies to its own claude -p timeouts.

    Bug fixed 2026-07-30 (found in an independent Codex code review): the
    original version only ever watched `proc.wait()` — i.e. whether the ONE
    direct child (the group leader) exited — and treated that as "the group
    is done." SIGTERM was sent to the whole group via `os.killpg`, but if the
    leader (e.g. a `claude -p` wrapper) happened to exit quickly while a
    grandchild it spawned (e.g. a `codex`/`agy` subprocess mid-write) was
    still shutting down, `proc.wait()` returned with no TimeoutError, so the
    SIGKILL escalation never ran — exactly the orphaned-mid-write scenario
    this function's own docstring claims to prevent. Now: after the leader
    exits (or times out), separately probe whether the ORIGINAL pgid (kept
    from before the leader exited — `os.getpgid(proc.pid)` would raise once
    that pid is gone) still has any live member via a signal-0 existence
    check, and only declare success once the whole group is confirmed empty.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        _kill_process_group(proc)
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        _force_kill_pgid(pgid)
        return
    try:
        os.killpg(pgid, 0)  # existence probe only — signal 0 never actually kills
    except (ProcessLookupError, PermissionError, OSError):
        return  # group is confirmed empty (or we can't check further)
    _force_kill_pgid(pgid)


REPO_LOCK_DIR = Path.home() / ".claude" / "discord-bot" / "repo-locks"


class RepoLockBusy(Exception):
    """Raised by try_acquire_repo_lock() when another PROCESS (not just
    another coroutine in this same process) already holds the lock for this
    resolved repo path."""


def _repo_lock_path(resolved_path: str) -> Path:
    # A hash, not the raw path, as the filename — resolved repo paths can be
    # long/contain characters awkward for a filename, and a hash keeps the
    # lock directory flat and collision-free without needing to sanitize.
    digest = hashlib.sha256(resolved_path.encode()).hexdigest()[:32]
    return REPO_LOCK_DIR / f"{digest}.lock"


@contextlib.contextmanager
def try_acquire_repo_lock(resolved_path: str):
    """Cross-process, non-blocking file lock keyed by a repo's resolved
    absolute path (2026-07-30, added in the same integration audit that
    fixed the !코덱스 double-fire bug).

    `CODEX_DISPATCH_LOCKS` (codex-bot.py's own asyncio.Lock dict) only
    protects against races WITHIN that one process — it has no visibility
    into discord-bot.py, a separate OS process with its own Python heap.
    Since discord-bot.py's verify-task-v2 retry handlers and codex-bot.py's
    `!코덱스`/`!코덱스대화` dispatch can both end up writing to the same
    resolved repo path (independently of the wake-word bug already fixed —
    e.g. two genuinely different user commands issued close together), a
    second, cross-process layer is needed for the same "reject immediately,
    never queue/wait" semantics the in-process locks already use.

    Uses `flock(2)` via a plain lock file under `REPO_LOCK_DIR`, one per
    resolved path (hashed filename). Non-blocking (`LOCK_NB`): raises
    `RepoLockBusy` immediately if any other process — this one's sibling
    coroutine included, though callers should keep using the cheaper
    in-process `asyncio.Lock` as the first check for that case — already
    holds it, rather than waiting. Usage:

        try:
            with try_acquire_repo_lock(str(cwd.resolve())):
                ... do the write-capable work ...
        except RepoLockBusy:
            await message.channel.send("다른 실행이 이미 이 저장소를 건드리고 있습니다 — 끝나면 다시 시도해주세요.")
    """
    REPO_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _repo_lock_path(resolved_path)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RepoLockBusy(resolved_path)
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


CROSS_BOT_CONTEXT_LIMIT = 20


async def fetch_cross_bot_context(channel, own_bot_id: int, limit: int = CROSS_BOT_CONTEXT_LIMIT) -> str:
    """Cross-bot channel-context bridge (2026-07-30, user-requested,
    explicit design intent — NOT a session merge).

    discord-bot.py's Claude free-chat and codex-bot.py's Codex chat each
    keep their own separate, native conversation continuity
    (`claude -p --resume <session_id>` / `codex exec resume <thread_id>`)
    — that stays untouched. What was missing: since both bots sit in the
    SAME Discord channel and each only ever feeds ITS OWN resumed
    session/thread as context, neither agent had any visibility into what
    the user discussed with the *other* one, even though a human reading
    the channel would see both halves of the conversation naturally.

    This reads the channel's own recent message history (discord.py
    already has this for free — no separate file bridge or config needed)
    and returns everything NOT authored by the calling bot itself, oldest
    first. That includes the sibling bot's own replies AND the user's
    messages to it — deliberately not trying to classify "was this message
    actually directed at the other bot," since that's a fuzzy judgment call
    (wake words, replies, plain commands) that a bounded raw-history dump
    sidesteps entirely. The caller's own bot messages are excluded because
    they're already redundant with what `--resume`/`resume <thread_id>`
    already carries.

    Bounded by `limit` (Discord API messages, not tokens) so this stays
    cheap and can't unboundedly grow a prompt regardless of how long the
    channel's history gets. Returns "" if there's nothing to show (e.g. a
    fresh channel, or a channel where only this bot has ever posted) — the
    caller should skip injecting the block entirely in that case rather
    than send an empty section header.
    """
    lines = []
    async for msg in channel.history(limit=limit):
        if msg.author.id == own_bot_id:
            continue
        content = (msg.content or "").strip()
        if not content:
            continue
        label = "봇" if msg.author.bot else "사용자"
        lines.append(f"[{label}] {content}")
    lines.reverse()  # channel.history() yields newest-first; want chronological
    return "\n".join(lines)
