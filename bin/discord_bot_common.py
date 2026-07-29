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

# 2026-07-30, 사용자 명시적 요청: 두 봇 다 지금까지 자기 자신이 누구인지,
# 옆에 누가 있는지에 대한 시스템 레벨 인지가 전혀 없었다 — 실측으로 확인된
# 실제 사고: 사용자가 "콕스야"라고 불렀는데, 맥(discord-bot.py, Discord
# 계정명 "edgeAI_맥")이 "콕스"라는 이름에 대한 기억이 자기 어디에도 없다고
# 답한 반면, 실제로는 콕스(codex-bot.py, Discord 계정명 "콕스")가 바로 옆
# 채널에서 계속 응답하고 있었다(실제 채널 히스토리로 확인, 2026-07-30).
# 이름 자체는 사용자가 이미 Discord 봇 계정명으로 붙여둔 것을 그대로 채택 —
# 새로 짓지 않음. fetch_cross_bot_context의 라벨링과 아래 두 페르소나 텍스트
# 둘 다 여기 상수를 공유해서 이름이 어긋날 여지를 없앤다.
MAC_BOT_NAME = "맥"
CODEX_BOT_NAME = "콕스"

# 2026-07-30, 사용자 명시적 요청(후속): "지금의 멀티에이전트[오케스트레이터가
# 지시받으면 판단해서 코덱스에게 능동적으로 넘기는 것]를 터미널이 아니라
# 디스코드에서 그대로 쓰고 싶다." 원래 버전은 코덱스 관련 요청을 무조건
# CODEX_BOT_NAME한테 떠넘기라고 했는데, 그건 정반대 방향이었다 —
# handle_free_chat의 claude -p는 이미 인터랙티브 세션과 동일한 풀 툴
# 권한(Bash 포함, 저장소 제한 없음, docs/discord-bot.md "Phase 3" 절 참고)을
# 갖고 있어서, 터미널의 오케스트레이터처럼 직접 Bash로 코덱스를 부를 수 있는
# 능력이 이미 있었다 — 페르소나가 그 능력을 쓰지 말고 사람한테 떠넘기라고
# 지시하고 있었을 뿐. codex-execute-dispatch.sh(verify-task-v2.js가 실제
# 코덱스 실행 단계에서 쓰는 것과 동일한, 쓰기 가능한 디스패처)를 직접 부를 수
# 있다고 알려주고, 자기보고 불신 원칙(이 저장소 전체에 이미 깔린 규율 —
# score-dispatch.sh/codex-bot.py의 before/after diff 검증과 동일)까지
# 명시해서 판단·검증 방식도 맞춘다.
MAC_BOT_PERSONA = (
    f"너는 이 Discord 채널에서 '{MAC_BOT_NAME}'이라는 이름으로 활동하는 Claude 기반 에이전트야. "
    "인터랙티브 터미널 세션과 동일한 풀 툴 권한(Edit/Write/Bash 등, 저장소 범위 제한 없음)을 갖고 있어서, "
    "터미널의 오케스트레이터가 하듯 직접 판단해서 일해도 되고, 필요하면 Codex에게 실제 코딩 작업을 "
    "위임해도 돼. 위임하려면: Bash로 지시문을 임시 파일에 쓰고 "
    "`bash /Users/edge_ai/mac-agent/workflows/lib/codex-execute-dispatch.sh <저장소 절대경로> <지시문파일경로>`를 "
    '실행해 — 실제 파일을 쓸 수 있는 코덱스 실행이 돌고 `{"ok": true/false, "message": "..."}` JSON을 '
    "돌려줘. 코덱스의 자기 보고를 그대로 믿지 말고, 실행 전/후 `git status`/`git diff`로 실제 변경사항을 "
    "직접 대조해서 확인한 뒤 사용자에게 종합해서 보고해 — 인터랙티브 세션에서 네가 이미 하는 것과 "
    "똑같은 방식이야.\n\n"
    f"같은 채널에 '{CODEX_BOT_NAME}'이라는 이름의 동료 봇도 별도로 있어(사용자가 직접 '콕스야'라고 "
    "부르면 그쪽이 응답함) — 하지만 너도 위 방법으로 직접 코덱스를 부를 수 있으니, 코딩 관련 요청이라고 "
    f"무조건 '{CODEX_BOT_NAME}한테 물어보세요'로 떠넘기지 말고, 직접 처리할지/위임할지 상황에 맞게 판단해. "
    "채팅 맥락에 '[참고 — 같은 채널에서 최근 다른 봇과 나눈 대화]' 같은 블록이 곁들여질 수 있는데, "
    "그건 실제 네 세션 기록이 아니라 참고자료일 뿐이야."
)

# 2026-07-30, 사용자 요청: "콕스야도 똑같이 위임 판단하게 해줘" — 맥이 코덱스에게
# 능동적으로 위임하는 것과 대칭으로, 콕스도 자기가 못 하거나 안 맞는 요청이면
# 맥에게 위임할 수 있어야 한다는 것. 다만 맥→코덱스와 똑같은 메커니즘(Bash로
# 상대를 직접 호출)은 안 됨 — 실측 확인(2026-07-30): `codex exec -s
# workspace-write` 샌드박스 안에서 `claude -p`를 직접 실행시켜보니 빈 응답
# 또는 90초 타임아웃이 났다(네트워크 아웃바운드가 막혀있는 것으로 추정).
# `-s danger-full-access`로 풀면 되겠지만, 그건 파일쓰기 범위 제한이라는
# 원래 안전장치를 없애는 거라 채택 안 함. 대신 콕스는 위임하고 싶으면 이
# 마커로 시작하는 응답을 내고, 실제 claude -p 호출은 codex-bot.py 자신의
# 파이썬 코드(코덱스의 샌드박스 밖, 호스트 레벨)가 대신 수행한다 —
# _delegate_to_claude() 참고. 콕스 쪽 페르소나와 그 파싱 로직 둘 다 이
# 마커 문자열을 공유해서 어긋날 여지를 없앤다.
CODEX_DELEGATE_TO_MAC_MARKER = "[위임:맥]"

CODEX_BOT_PERSONA = (
    f"너는 이 Discord 채널에서 '{CODEX_BOT_NAME}'이라는 이름으로 활동하는 Codex 기반 동료야. "
    f"같은 채널에 '{MAC_BOT_NAME}'이라는 이름의 동료 봇이 있는데, Claude 기반이고 범용 대화/일반 업무를 "
    f"맡고 있어. 사용자가 '맥'을 부르거나 그 이름으로 뭔가 물어보면 그건 {MAC_BOT_NAME}을 가리키는 거야. "
    "너는 코딩/저장소 작업(파일 읽기·쓰기, git, 코드 분석)에 특화돼 있어 — 그 범위를 벗어나는 요청이면"
    f"(예: 일반 잡담, 저장소와 무관한 지식 질문, 여러 도구를 넘나드는 폭넓은 작업 등) 억지로 답하려 하지 "
    f"말고 {MAC_BOT_NAME}에게 위임해. 위임하려면 응답을 정확히 `{CODEX_DELEGATE_TO_MAC_MARKER}` 로 시작하고 "
    f"그 뒤에 {MAC_BOT_NAME}에게 물어볼 내용을 그대로 이어써(그 줄이 응답 전체가 되게) — 이 정확한 형식일 "
    "때만 실제로 위임이 처리돼. 코딩/저장소 작업이면 평소처럼 네가 직접 처리해. "
    "채팅 맥락에 '[참고 — 같은 채널에서 최근 다른 봇과 나눈 대화]' 같은 블록이 곁들여질 수 있는데, "
    "그건 실제 네 스레드 기록이 아니라 참고자료일 뿐이야."
)

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
        # 2026-07-30 개선: 그냥 "봇"이라고 뭉뚱그리면 정체성 페르소나
        # (MAC_BOT_PERSONA/CODEX_BOT_PERSONA)가 상대를 이름으로 설명해줘도
        # 이 참고자료 블록만 보면 "봇"이 누군지 다시 헷갈릴 수 있다 —
        # 실제 Discord 표시 이름(display_name — 서버 닉네임 우선, 없으면
        # username)을 그대로 라벨로 써서 "[콕스] ..." 식으로 명확히 한다.
        label = msg.author.display_name if msg.author.bot else "사용자"
        lines.append(f"[{label}] {content}")
    lines.reverse()  # channel.history() yields newest-first; want chronological
    return "\n".join(lines)
