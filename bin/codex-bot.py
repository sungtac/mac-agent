#!/usr/bin/env python3
"""Codex-dedicated Discord bot (2026-07-29) — split out of discord-bot.py
(the "맥" bot) at the user's explicit request so Codex-related commands live
under their own bot identity, separate from Claude-side commands
(주간보고서/상태/자유채팅), which stay on discord-bot.py.

Runs as its own persistent process under launchd (KeepAlive), same shape as
discord-bot.py — holds its own Discord Gateway WebSocket connection with its
own bot token. Shares discord_bot_common.py (SUBPROCESS_ENV, usage_gate_check,
_kill_process_group_graceful) with discord-bot.py rather than duplicating
those, so a fix to either lands for both bots.

Config: ~/.claude/discord-bot/codex-bot-config.json —
{"token": "...", "channel_id": "...", "free_chat_user_id": "..."}
Deliberately a SEPARATE file from discord-bot.py's config.json (own token,
independently rotatable) even though channel_id/free_chat_user_id are
expected to hold the same values in practice (same channel, same authorized
person) — duplicated on purpose so each bot script stays fully
self-contained and independently runnable, matching discord-bot.py's own
single-file-per-bot convention. Not committed to this (public) repo.

Commands (moved here verbatim from discord-bot.py, both restricted to
FREE_CHAT_USER_ID — this is real workspace-write access, not read-only chat):
- `!코덱스 <repo-alias> <task>` — one-shot dispatch, see handle_codex_dispatch.
- `!코덱스대화 <repo-alias> <message>` — ongoing ChatGPT/`codex` CLI-style
  conversation via Codex's native `exec resume`, see handle_codex_chat.
- `!코덱스대화초기화 <repo-alias>` — reset that alias's conversation.
"""
import asyncio
import datetime
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import discord

from discord_bot_common import MAC_AGENT, SUBPROCESS_ENV, is_codex_wake_word, is_group_address, usage_gate_check, _kill_process_group_graceful, try_acquire_repo_lock, RepoLockBusy, fetch_cross_bot_context, CODEX_BOT_PERSONA, MAC_BOT_PERSONA, MAC_BOT_NAME, CODEX_DELEGATE_TO_MAC_MARKER, QUOTA_LIMIT_PATTERN

CONFIG_PATH = Path.home() / ".claude" / "discord-bot" / "codex-bot-config.json"
CODEX_EXECUTE_DISPATCH_SH = MAC_AGENT / "workflows" / "lib" / "codex-execute-dispatch.sh"
CODEX_BIN = Path("/opt/homebrew/bin/codex")  # absolute, not bare `codex` — same PATH-under-launchd gotcha as every other subprocess binary in this repo
PROVIDER_SANDBOX = Path(__file__).resolve().with_name("edge-agent-provider-sandbox.sh")
# 2026-07-30, 콕스→맥 위임(CODEX_DELEGATE_TO_MAC_MARKER)용 — 콕스 자신의
# 샌드박스 밖(이 파일, 즉 codex-bot.py 자신의 호스트 레벨 프로세스)에서
# 직접 부른다. discord-bot.py도 똑같은 절대경로를 자기 파일에 따로 갖고
# 있음(CODEX_BIN처럼 이 저장소에서 이미 흔한 패턴 — 완전 중앙화는 안 함).
CLAUDE_BIN = Path.home() / ".local" / "bin" / "claude"
CLAUDE_DELEGATE_TIMEOUT_SECONDS = 30 * 60  # !코덱스/코덱스대화와 동일한 관대한 여유

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

# Concurrency guard: two overlapping `!코덱스`/`!코덱스대화` runs against the
# SAME repo would both get workspace-write access at once, and
# _dirty_snapshot()'s own before/after diff already admits it can't fully
# attribute changes made by another process editing DURING its window — this
# bot itself could be that other process. Keyed per resolved cwd (not the
# alias string, not one global lock) since two DIFFERENT repos genuinely
# don't share any state and running them concurrently is fine. Shared between
# handle_codex_dispatch and handle_codex_chat/handle_codex_chat_reset so a
# one-shot dispatch and a chat turn against the same repo can't race either.
CODEX_DISPATCH_LOCKS: dict[str, asyncio.Lock] = {}

# 2026-07-30 fix (사용자 확정, 기능 패리티 갭): discord-bot.py엔 자유채팅을
# 중간에 멈추는 !중지가 있는데 이쪽엔 대응 명령이 없었다 — 오래 걸리는
# !코덱스/!코덱스대화를 멈추려면 타임아웃(30분)을 그냥 기다려야 했다.
# discord-bot.py의 FREE_CHAT_CURRENT_PROC과 같은 패턴이지만, 여러 별칭이
# 동시에 독립적으로 돌 수 있으므로 단일 전역이 아니라 lock_key(정규화된
# cwd)별 dict로 관리한다.
CODEX_CURRENT_PROCS: dict[str, asyncio.subprocess.Process] = {}


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
# Channel trust alone is not enough for commands that let a channel member
# cause arbitrary code execution (workspace-write) — same posture as
# discord-bot.py's !코덱스. Empty/missing = fail closed (nobody passes).
FREE_CHAT_USER_ID = str(CONFIG.get("free_chat_user_id") or "")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def _git_output(cwd: Path, *args: str) -> str:
    # env=SUBPROCESS_ENV added (2026-07-30, found in integration audit): the
    # only subprocess spawn point in either bot file that omitted this —
    # every other spawn call explicitly passes it. Works today since git is
    # invoked by absolute path, but would silently diverge from the
    # documented mandatory pattern the moment this repo's git config ever
    # references a homebrew-installed pager/credential-helper/diff-tool by
    # bare name (same PATH gotcha as codex/agy/tmux/coach elsewhere).
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/git", "-C", str(cwd), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=SUBPROCESS_ENV,
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="replace").strip()
    if proc.returncode != 0:
        # 2026-07-29 fix: this used to return stderr text mixed into `out`
        # as if it were legitimate diff/status output, with no returncode
        # check at all. _dirty_snapshot()'s line-parsing only recognizes
        # lines starting with "diff --git " or "?? " — a git failure (e.g.
        # `.git/index.lock` held by a concurrent process, not a repo, a
        # permissions error) produces neither, so the error text was
        # silently dropped and the snapshot came back as an empty `{}` —
        # indistinguishable from "genuinely no changes". That's the one
        # thing this whole module exists to prevent: handle_codex_dispatch's
        # own docstring says Codex's self-report is "never trusted... this
        # function independently diffs the repo" — but a swallowed git
        # failure meant that independent check could silently report "실제
        # 파일 변경 없음" when the truth was "git itself failed, unknown
        # state". Raising here lets it propagate to the existing outer
        # `except Exception as e` in handle_codex_dispatch/_codex_chat_turn,
        # which already reports failures to the user — no new handling
        # needed at the call sites.
        raise RuntimeError(f"git {' '.join(args)} failed (exit={proc.returncode}): {text[:500]}")
    return text


def _hash_file_content(path: Path) -> str:
    """Sync on purpose — callers must `await asyncio.to_thread(...)` this: it
    runs inside `_dirty_snapshot`, an async function with no `await` around
    the hashing itself, so a large untracked file (e.g. a stray big binary)
    would block the bot's entire event loop — no other message gets
    processed until the hash finishes. Kept sync + plain here so it stays
    trivially testable; the thread offload lives at the call site instead.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        # Unreadable (permissions, vanished between listing and reading,
        # etc.) — a fixed marker still lets before/after comparison detect
        # a CHANGE (unreadable -> readable or vice versa), just not what
        # changed. Never raise out of a snapshot helper.
        return "UNREADABLE"


# Cap for the untracked-directory walk below: a repo where a heavy directory
# (build output, a dependency tree) was left untracked with no .gitignore
# entry would otherwise get every one of its files read + hashed on every
# single dispatch/chat turn. None of today's CODEX_REPO_ALIASES currently has
# an untracked directory at all (checked live), so this is a latent risk
# rather than an observed one — the cap exists so a future misconfigured
# repo degrades to a cheap approximation instead of an unbounded full-content
# hash of everything inside.
UNTRACKED_DIR_HASH_FILE_CAP = 2000


def _list_files_under(dir_path: Path) -> list:
    """Sync — same to_thread-at-call-site rule as _hash_file_content; walking
    a directory tree is real (if usually fast) filesystem I/O and belongs off
    the event loop for the same reason hashing does."""
    return sorted(p for p in dir_path.rglob("*") if p.is_file())


def _stat_signature(cwd: Path, files: list) -> str:
    """Cheap stand-in for per-file content hashing when an untracked
    directory has more than UNTRACKED_DIR_HASH_FILE_CAP files: aggregates
    (relative path, size, mtime) instead of reading every file's actual
    bytes. Still catches adds/removes/renames and almost all real edits (a
    changed file's mtime moves), just not a content edit that somehow
    preserves both size and mtime — an acceptable trade for not doing
    unbounded I/O on every dispatch against a repo with one huge untracked
    directory."""
    agg = hashlib.sha256()
    for sub in files:
        try:
            st = sub.stat()
            agg.update(f"{sub.relative_to(cwd).as_posix()}:{st.st_size}:{st.st_mtime_ns}\n".encode())
        except OSError:
            agg.update(b"STATERR\n")
    return agg.hexdigest()


async def _dirty_snapshot(cwd: Path) -> dict:
    """filename -> its current unified-diff text for a tracked change, or
    "UNTRACKED:<sha256 of file content>" for an untracked file, for every
    file the working tree currently shows as changed OR that exists
    untracked. Used to compute a before/after delta around a Codex run
    instead of trusting a single post-run `git diff --stat` — a repo can
    have other uncommitted work in flight (another terminal, a concurrent
    session) that has nothing to do with this specific dispatch, and a bare
    post-run diff cannot tell the two apart.

    Untracked entries carry a content hash, not a bare "UNTRACKED" marker: a
    flat marker only tells you a path IS untracked, not what's in it, which
    would hide (1) Codex modifying the CONTENT of an already-untracked file
    without `git add`ing it, and (2) Codex DELETING a previously-untracked
    file (it vanishes from both `git diff` and `git status --porcelain`
    entirely). Hashing the actual bytes makes both cases produce a real
    before/after difference like any tracked change would.
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
            path = line[3:]
            full = cwd / path
            if path.endswith("/"):
                # git collapses an entirely-untracked directory into a single
                # "?? dir/" line rather than listing its files individually —
                # _hash_file_content(full) would call read_bytes() on a
                # directory, hit IsADirectoryError, and fall back to the
                # fixed "UNREADABLE" marker every time, so before/after both
                # said "UNTRACKED:UNREADABLE" regardless of what changed
                # inside. Walk it and hash each file individually, keyed by
                # its path relative to cwd — same shape as a top-level
                # untracked file, so the generic changed/changed_tracked/
                # untracked_notes logic below needs no changes to pick these
                # up per-file. Above UNTRACKED_DIR_HASH_FILE_CAP files, fall
                # back to one cheap aggregate stat-based signature instead
                # (see that constant's comment) — the "UNTRACKED:" prefix is
                # kept so _is_untracked_marker() below still recognizes it.
                sub_files = await asyncio.to_thread(_list_files_under, full)
                if len(sub_files) > UNTRACKED_DIR_HASH_FILE_CAP:
                    sig = await asyncio.to_thread(_stat_signature, cwd, sub_files)
                    snapshot[path] = f"UNTRACKED:dircap({len(sub_files)}):{sig}"
                else:
                    for sub in sub_files:
                        rel = sub.relative_to(cwd).as_posix()
                        snapshot[rel] = f"UNTRACKED:{await asyncio.to_thread(_hash_file_content, sub)}"
            else:
                snapshot[path] = f"UNTRACKED:{await asyncio.to_thread(_hash_file_content, full)}"
    return snapshot


async def _diff_summary(cwd: Path, before: dict, after: dict) -> str:
    """Renders the file-level diff between two _dirty_snapshot() results as
    a display string (git diff --stat for tracked changes + notes for
    untracked adds/deletes/edits). Pulled out of handle_codex_dispatch's and
    _codex_chat_turn's success paths (2026-07-29) — both had this exact block
    duplicated verbatim, and neither used it on a timeout kill, so a forced
    termination reported only "직접 확인해주세요" with zero information about
    what had actually changed before the kill. Sharing this lets both the
    success path AND the timeout path show the same real diff.
    """
    # Union of before/after keys, not just after's: a file that was
    # UNTRACKED in `before` and no longer appears in `after` at all
    # (deleted) would otherwise be silently invisible. A tracked file
    # reverted to exactly its committed state has the same shape and
    # is correctly caught by this same union-based comparison.
    all_files = set(before) | set(after)
    changed = sorted(f for f in all_files if before.get(f) != after.get(f))

    def _is_untracked_marker(v) -> bool:
        return isinstance(v, str) and v.startswith("UNTRACKED:")

    changed_tracked = [
        f for f in changed
        if not _is_untracked_marker(before.get(f)) and not _is_untracked_marker(after.get(f))
    ]
    untracked_notes = []
    for f in changed:
        if f in changed_tracked:
            continue
        if f not in before:
            untracked_notes.append(f"신규 파일: {f}")
        elif f not in after:
            untracked_notes.append(f"삭제됨(기존 미추적 파일): {f}")
        else:
            untracked_notes.append(f"내용 변경(미추적 파일): {f}")

    tracked_stat = ""
    if changed_tracked:
        tracked_stat = await _git_output(cwd, "diff", "--stat", "--", *changed_tracked)
    notes_block = "\n".join(untracked_notes) if untracked_notes else ""
    # Capped here, not left to the callers (2026-07-30 fix — found in
    # integration audit): this return value has no length limit of its own,
    # unlike every other field the two callers compose into their final
    # message (e.g. codex_message is already tail-sliced to [-1000:]).
    # Both callers join this into a "lines" list and only truncate the FULL
    # joined string to [:1900] at the very end — since this field sits
    # BEFORE the deliberately-preserved tail content in that list, an
    # oversized diff_stat pushes the join past 1900 chars and the outer
    # slice blind-truncates from the FRONT, silently eating into or
    # dropping the carefully-preserved tail content after it. This is
    # exactly the "second truncation layer undoing the prior tail-fix" bug
    # class already fixed once in discord-bot.py (see git log).
    #
    # Bug fixed 2026-07-30 (found in an independent Codex code review, same
    # day as the first cap): the first version of this cap took the FULL
    # joined string (tracked stat + untracked notes) and tail-sliced it as
    # one unit, on the assumption that `git diff --stat`'s one genuinely
    # load-bearing line — "N files changed, M insertions..." — is always
    # last. That's only true when there are no untracked_notes. Since notes
    # are appended AFTER tracked_stat, a nontrivial untracked_notes block
    # pushes the real summary line out of the tail-sliced window entirely
    # (reproduced: 60 untracked-file notes alone exceed 600 chars, leaving
    # zero room for the tracked summary). Fix: cap each part separately with
    # its own budget, so neither can crowd the other out of existence.
    tracked_stat_capped = tracked_stat[-400:]
    notes_capped = notes_block[-200:]
    return "\n".join(p for p in (tracked_stat_capped, notes_capped) if p)


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
    `git diff --stat`, since another process editing the same working tree
    concurrently would otherwise get misattributed to this run.
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

    # Keyed by resolved path, not the alias string: CODEX_REPO_ALIASES has no
    # distinct-paths invariant enforced anywhere — if two aliases ever
    # pointed at the same actual directory, locking per-alias-name would let
    # runs against them race each other. Resolving symlinks/`..` first means
    # two aliases for the same real repo always collide on the same lock
    # object regardless of which name was used to reach it.
    lock_key = str(cwd.resolve())
    lock = CODEX_DISPATCH_LOCKS.setdefault(lock_key, asyncio.Lock())
    if lock.locked():
        await message.channel.send(f"`{alias}`에 대한 다른 `!코덱스` 실행이 이미 진행 중입니다 — 끝나면 다시 시도해주세요.")
        return

    # 2026-07-30 fix (사용자 확정, Codex 코드리뷰로 발견): 위 asyncio.Lock은
    # 이 codex-bot.py 프로세스 안에서만 유효하다 — discord-bot.py의
    # verify-task-v2 재시도가 다른 프로세스에서 같은 저장소를 거의 동시에
    # 건드리는 경우까지는 못 막았다(!코덱스 이중발동 버그는 이미 닫혔지만,
    # 서로 다른 명령이 우연히 겹치는 경우는 여전히 가능). 파일 기반 락으로
    # 프로세스 경계를 넘어 한 번 더 확인.
    try:
        with try_acquire_repo_lock(lock_key):
            async with lock:
                await _handle_codex_dispatch_locked(message, alias, cwd, task)
    except RepoLockBusy:
        await message.channel.send(f"`{alias}`에 대한 다른 프로세스의 실행이 이미 진행 중입니다 — 끝나면 다시 시도해주세요.")
    return


async def _handle_codex_dispatch_locked(message: discord.Message, alias: str, cwd: Path, task: str) -> None:
    # No pending-job/requeue here — !코덱스 is a one-shot manual command,
    # not a reply-triggered retry chain, so there's nothing to requeue:
    # the user just re-sends !코덱스 later.
    skip_reason = await usage_gate_check("codex")
    if skip_reason:
        await message.channel.send(f"⏳ 지금 실행을 건너뜁니다 — 계정 사용량 부족.\n{skip_reason}\n사용량 회복 후 `!코덱스` 명령을 다시 보내주세요.")
        return

    # `before` inside the try (not before it): an exception here (e.g. a
    # git subprocess failure) must not propagate out of this handler
    # uncaught — on_message has no wrapping try/except of its own, so the
    # user would get total silence, not even the "코덱스에게
    # 지시했습니다" starting message, let alone an error.
    prompt_file = None
    try:
        before = await _dirty_snapshot(cwd)
        dirty_note = "\n⚠️ 이 저장소에 이미 커밋 안 된 변경사항이 있습니다 — 최종 결과는 이번 실행분만 골라 보여드릴게요." if before else ""

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(task)
            prompt_file = Path(f.name)

        await message.channel.send(f"코덱스에게 지시했습니다 ({alias}, 최대 {CODEX_DISPATCH_TIMEOUT_SECONDS // 60}분 정도 걸릴 수 있어요) — 끝나면 알려드릴게요.{dirty_note}")

        # start_new_session=True + _kill_process_group_graceful():
        # codex-execute-dispatch.sh runs the real `codex exec` as
        # `RAW_OUTPUT="$(codex exec ...)"` — a command substitution, so
        # codex is a CHILD of this bash process, not the process itself.
        # A plain proc.kill() here only kills the bash wrapper, leaving
        # the actual codex process (full workspace-write access) running
        # undetected in the background.
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", str(CODEX_EXECUTE_DISPATCH_SH), str(cwd), str(prompt_file),
            env=SUBPROCESS_ENV,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        CODEX_CURRENT_PROCS[str(cwd.resolve())] = proc
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=CODEX_DISPATCH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await _kill_process_group_graceful(proc)
            # 2026-07-29 fix: show the partial diff up to the kill point
            # instead of only "직접 확인해주세요" with zero information —
            # `before` was already captured above, so this is the same
            # before/after comparison the success path below does, just
            # triggered by a forced kill instead of a clean finish.
            after = await _dirty_snapshot(cwd)
            diff_stat = await _diff_summary(cwd, before, after)
            timeout_msg = f"⚠️ 코덱스 실행이 {CODEX_DISPATCH_TIMEOUT_SECONDS // 60}분을 넘어서 강제 종료했습니다."
            if diff_stat:
                timeout_msg += f" 강제종료 직전까지 실제 변경된 파일(중간에 끊긴 상태일 수 있음):\n```\n{diff_stat}\n```"
            else:
                timeout_msg += f" 강제종료 시점까지 감지된 파일 변경은 없습니다 — {alias} 저장소를 직접 확인해주세요."
            await message.channel.send(timeout_msg[:1900])
            return

        raw = (stdout or b"").decode(errors="replace")
        try:
            result = json.loads(raw)
            ok = bool(result.get("ok"))
            # tail, not head: codex-execute-dispatch.sh already returns
            # its message tail-truncated so it ends at Codex's actual
            # failure reason — cutting the FIRST 1000 chars here instead
            # would re-discard the ending it just preserved.
            codex_message = str(result.get("message", ""))[-1000:]
        except Exception:
            ok = False
            codex_message = f"(codex-execute-dispatch.sh 출력이 JSON이 아님) {raw[-1000:]}"

        # Never trust Codex's own report — confirm with a real before/after diff.
        after = await _dirty_snapshot(cwd)
        diff_stat = await _diff_summary(cwd, before, after)

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
        CODEX_CURRENT_PROCS.pop(str(cwd.resolve()), None)
        if prompt_file is not None:
            prompt_file.unlink(missing_ok=True)


# `!코덱스대화` — a ChatGPT/`codex` CLI-style ongoing conversation, distinct
# from !코덱스's one-shot "dispatch one task, report, done". Codex itself
# supports this natively — `codex exec --json` prints a
# `{"type":"thread.started","thread_id":...}` event as its first line, and
# `codex exec resume <thread_id> --json <prompt>` continues that same
# conversation (verified live: a fact given in one message was correctly
# recalled in the next). Session state is kept PER ALIAS (not one global
# session) since a Codex thread is tied to the cwd it started in — switching
# aliases mid-conversation should start a distinct thread for that other
# repo, not try to resume one rooted somewhere else.
CODEX_CHAT_SESSION_DIR = Path.home() / ".claude" / "discord-bot" / "codex-chat-sessions"
CODEX_CHAT_TIMEOUT_SECONDS = 30 * 60  # same budget as !코덱스 — a chat turn can still be a real coding task
# 2026-07-30, 사용자 확정("낮춤(medium으로) — 채팅/wake-word만"): 실측으로
# 확인된 실제 문제 — "콕스야" -> "응, 콕스 여기 있어" 같은 한 줄짜리 인사에도
# 19.5~19.8초가 걸렸다(Discord 메시지 타임스탬프 직접 대조로 확인). 원인은
# ~/.codex/config.toml의 전역 `model_reasoning_effort = "high"` — 코딩
# 작업엔 맞는 설정이지만 캐주얼한 채팅 응답에도 똑같이 적용되고 있었다.
# `!코덱스`(handle_codex_dispatch, codex-execute-dispatch.sh 경유)와
# verify-task-v2 위임은 전역 high 설정을 그대로 쓰도록 안 건드림 — 이
# 오버라이드는 _codex_chat_turn_locked(대화형 채팅/wake-word 경로)에만
# 적용. `-c 'model_reasoning_effort="medium"'`(TOML 문자열 값 명시적 인용
# — 인용 없이 넘기면 CLI가 "TOML 파싱 실패 시 리터럴로 처리"하는 폴백
# 경로에 기대게 되므로, 신뢰도를 위해 명시적으로 인용) 실측으로 실제
# "reasoning effort: medium"이 반영되는 것 확인.
CODEX_CHAT_REASONING_EFFORT_ARGS = ["-c", 'model_reasoning_effort="medium"']


def _codex_chat_session_path(alias: str) -> Path:
    return CODEX_CHAT_SESSION_DIR / f"{alias}.json"


def _load_codex_chat_thread_id(alias: str) -> str | None:
    try:
        return json.loads(_codex_chat_session_path(alias).read_text()).get("thread_id")
    except Exception:
        return None


def _save_codex_chat_thread_id(alias: str, thread_id: str) -> None:
    CODEX_CHAT_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    _codex_chat_session_path(alias).write_text(json.dumps({
        "thread_id": thread_id,
        "last_used_at": datetime.datetime.now().isoformat(),
    }))


def _parse_codex_json_events(raw: str) -> tuple[str | None, str]:
    """Parse `codex exec --json`/`codex exec resume --json` stdout (one JSON
    object per line) into (thread_id, reply_text).

    reply_text joins EVERY `item.completed` event with `item.type ==
    "agent_message"` in order, not just the last one — confirmed live that a
    single turn with a file write emits an intro agent_message ("파일을
    만들겠습니다"), then a `file_change` item, then a closing agent_message
    ("만들었습니다") — taking only the last would silently drop the intro.
    thread_id comes from the `thread.started` event, present on both a fresh
    run and a resumed one (confirmed live) with the SAME id on resume, so the
    caller can always just re-save whatever id this returns.

    Malformed/non-JSON lines are skipped rather than raising — `codex exec`
    can print a "Reading additional input from stdin..." notice before the
    JSON stream starts (observed live), and a caller here must never crash a
    chat turn just because output had a stray non-JSON line.
    """
    thread_id = None
    texts = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        etype = event.get("type")
        if etype == "thread.started":
            thread_id = event.get("thread_id")
        elif etype == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                texts.append(item["text"])
    return thread_id, "\n\n".join(texts)


async def handle_codex_chat_reset(message: discord.Message):
    """!코덱스대화초기화 <저장소별칭> — deletes that alias's saved thread id
    so the next !코덱스대화 for it starts a brand-new Codex conversation,
    same shape as discord-bot.py's handle_free_chat_reset (!새대화)."""
    if str(message.author.id) != FREE_CHAT_USER_ID:
        await message.channel.send("이 명령어는 본인만 사용 가능합니다.")
        return

    parts = message.content.strip().split(maxsplit=1)
    aliases = ", ".join(sorted(CODEX_REPO_ALIASES))
    if len(parts) < 2:
        await message.channel.send(f"사용법: `!코덱스대화초기화 <저장소별칭>`\n사용 가능한 별칭: {aliases}")
        return

    alias = parts[1].strip()
    if alias not in CODEX_REPO_ALIASES:
        await message.channel.send(f"알 수 없는 저장소 별칭: `{alias}`\n사용 가능한 별칭: {aliases}")
        return

    try:
        # Block while a turn for this alias is in flight, so that turn's own
        # eventual _save_codex_chat_thread_id() call can't land after this
        # delete and silently undo the reset.
        lock_key = str(CODEX_REPO_ALIASES[alias].resolve())
        lock = CODEX_DISPATCH_LOCKS.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await message.channel.send(f"`{alias}`에 대한 실행이 진행 중이라 초기화할 수 없습니다 — 끝난 뒤 다시 시도해주세요.")
            return

        _codex_chat_session_path(alias).unlink(missing_ok=True)
        await message.channel.send(f"`{alias}` 코덱스 대화를 초기화했습니다 — 다음 메시지부터 새 대화로 시작합니다.")
    except Exception as e:
        # This handler previously had no try/except at all — on_message has
        # no wrapping try/except of its own, so an exception here (e.g. a
        # permissions error on the session file) meant total silence, the
        # one path in this codebase that broke its own "무응답 절대 금지"
        # rule. Same posture as handle_codex_dispatch/_codex_chat_turn below.
        await message.channel.send(f"❌ 코덱스대화 초기화 중 예외: {e}")


async def handle_codex_chat(message: discord.Message):
    """`!코덱스대화 <저장소별칭> <메시지>` — an ongoing, write-capable Codex
    conversation (ChatGPT/`codex` CLI style), distinct from !코덱스's
    one-shot "dispatch one task, report, done". See the module comment above
    _parse_codex_json_events for how Codex's own native thread resume makes
    this possible. Restricted to CODEX_REPO_ALIASES and FREE_CHAT_USER_ID —
    same trust boundary as !코덱스, since this is real workspace-write
    access, not a read-only chat.

    Parses `<저장소별칭> <메시지>` and delegates to _codex_chat_turn — kept
    separate from handle_codex_chat_wake (2026-07-29) so the two entry
    points (explicit command vs. natural wake-word chat, see that function)
    share the exact same execution path and can't silently drift.
    """
    if str(message.author.id) != FREE_CHAT_USER_ID:
        await message.channel.send("이 명령어는 본인만 사용 가능합니다.")
        return

    parts = message.content.strip().split(maxsplit=2)
    aliases = ", ".join(sorted(CODEX_REPO_ALIASES))
    if len(parts) < 3:
        await message.channel.send(f"사용법: `!코덱스대화 <저장소별칭> <메시지>`\n사용 가능한 별칭: {aliases}")
        return

    _, alias, text = parts
    await _codex_chat_turn(message, alias, text)


CODEX_CHAT_DEFAULT_ALIAS = "mac-agent"  # natural chat has no room to specify an alias each turn; use !코덱스대화 <다른 별칭> explicitly to target hwpx-skill/pptx-skill instead


async def handle_codex_chat_wake(message: discord.Message):
    """Natural, prefix-less chat: any message addressing Codex by name (see
    is_codex_wake_word() in discord_bot_common.py — leading or trailing
    token, e.g. "코덱스야 ...", "콕스 ...", or "안녕 콕스") is treated as a
    !코덱스대화 turn against CODEX_CHAT_DEFAULT_ALIAS, no command syntax
    required (2026-07-29, user's explicit request — "ChatGPT처럼" addressing
    Codex by name in ongoing chat rather than typing `!코덱스대화 <alias>`
    every time). The FULL original message (including the wake word itself,
    e.g. "코덱스야, 마크다운 파일 하나 만들어줘") is sent to Codex as-is —
    Codex is a language model, not a parser, so there's no need to strip the
    vocative prefix out first.

    discord-bot.py's own free-chat catch-all excludes messages matching the
    same is_codex_wake_word() check (see its on_message) so only one of the
    two bots answers a given wake-worded message, not both.
    """
    if str(message.author.id) != FREE_CHAT_USER_ID:
        return  # silently ignore — same posture as discord-bot.py's free-chat fallthrough for anyone else in the channel
    text = message.content.strip()
    if not text:
        return
    await _codex_chat_turn(message, CODEX_CHAT_DEFAULT_ALIAS, text)


def _codex_chat_reset_hint(alias: str, existing_thread_id: str | None) -> str:
    """Appended to resume-turn failure/timeout messages so a broken saved
    thread_id doesn't repeat the same failure forever with no documented way
    out — only meaningful when there IS a saved thread to reset (a brand-new
    first turn has nothing to point at yet)."""
    if not existing_thread_id:
        return ""
    return f" 계속 실패하면 `!코덱스대화초기화 {alias}`로 초기화해보세요."


async def _codex_chat_turn(message: discord.Message, alias: str, text: str) -> None:
    """Shared body for both handle_codex_chat (explicit `!코덱스대화 <별칭>`)
    and handle_codex_chat_wake (natural "코덱스야 ..." chat) — everything
    from alias validation through Codex execution and diff reporting is
    identical between the two entry points; only how (alias, text) got
    determined differs.

    Shares CODEX_DISPATCH_LOCKS with handle_codex_dispatch, keyed the same
    way (resolved cwd) — a !코덱스 one-shot and a chat turn against the SAME
    repo must never run concurrently. Also reuses _dirty_snapshot
    before/after verification and _kill_process_group_graceful for timeouts —
    Codex's own self-report is never trusted here either.

    No -s/-C on resume (verified live): `codex exec resume` doesn't accept
    either flag — a resumed thread keeps the sandbox/cwd it was created with,
    so those only get passed on the FIRST turn of a new thread.
    """
    aliases = ", ".join(sorted(CODEX_REPO_ALIASES))
    cwd = CODEX_REPO_ALIASES.get(alias)
    if cwd is None:
        await message.channel.send(f"알 수 없는 저장소 별칭: `{alias}`\n사용 가능한 별칭: {aliases}")
        return

    lock_key = str(cwd.resolve())
    lock = CODEX_DISPATCH_LOCKS.setdefault(lock_key, asyncio.Lock())
    if lock.locked():
        await message.channel.send(f"`{alias}`에 대한 다른 코덱스 실행이 이미 진행 중입니다 — 끝나면 다시 시도해주세요.")
        return

    # 2026-07-30 fix (사용자 확정, Codex 코드리뷰로 발견): handle_codex_dispatch와
    # 동일한 이유로 크로스프로세스 파일 락 추가.
    try:
        with try_acquire_repo_lock(lock_key):
            async with lock:
                await _codex_chat_turn_locked(message, alias, text, cwd)
    except RepoLockBusy:
        await message.channel.send(f"`{alias}`에 대한 다른 프로세스의 실행이 이미 진행 중입니다 — 끝나면 다시 시도해주세요.")


async def _codex_chat_turn_locked(message: discord.Message, alias: str, text: str, cwd: Path) -> None:
    skip_reason = await usage_gate_check("codex")
    if skip_reason:
        await message.channel.send(f"⏳ 지금 실행을 건너뜁니다 — 계정 사용량 부족.\n{skip_reason}\n사용량 회복 후 다시 시도해주세요.")
        return

    try:
        before = await _dirty_snapshot(cwd)
        dirty_note = "\n⚠️ 이 저장소에 이미 커밋 안 된 변경사항이 있습니다 — 최종 결과는 이번 턴만 골라 보여드릴게요." if before else ""

        # 2026-07-30, 사용자 명시적 요청("이 부분을 기억해달라"): 코덱스 스레드
        # (exec resume)는 그대로 독립 유지하되(세션 병합 아님), 같은 채널에서
        # discord-bot.py(Claude 자유채팅)와 나눈 대화를 이 턴의 프롬프트에
        # 참고자료로 얹어준다 — discord-bot.py의 handle_free_chat과 대칭.
        cross_context = await fetch_cross_bot_context(message.channel, client.user.id)
        if cross_context:
            prompt_text = (
                "[참고 — 같은 Discord 채널에서 최근 다른 봇(Claude)과 나눈 대화. "
                "네 실제 스레드 기록이 아니라 곁눈질로 보는 참고자료일 뿐이니, "
                "여기 내용을 사실로 단정하지 말고 필요할 때만 자연스럽게 참고해:]\n"
                f"{cross_context}\n\n[사용자 메시지]\n{text}"
            )
        else:
            prompt_text = text

        # 2026-07-30, 실측 버그: CODEX_BOT_PERSONA의 "상대를 대신 소개하지
        # 마라" 지침은 새 스레드 첫 턴에만 들어간다(위 else 분기, 스레드
        # 노이즈 방지 목적) — 그런데 사용자가 실제로 겪은 문제는 이미
        # 진행 중이던 기존 스레드(resume)에서 발생했고, 거기엔 그 지침이
        # 아예 없어서 재기동으로도 반영이 안 됐다. resume/새 스레드 여부와
        # 무관하게, 이번 턴이 그룹 지칭이면 매번 짧게 재주입한다 — 스레드
        # 히스토리에 계속 쌓이긴 하지만 그룹 지칭 요청 자체가 매번 발생하는
        # 것이므로 노이즈보다 정확성이 우선.
        if is_group_address(text):
            prompt_text = (
                f"[중요 — 이번 요청은 '둘 다'/'각자'/'모두'처럼 여러 참가자를 한꺼번에 지칭한다. "
                f"{MAC_BOT_NAME}을 대신해서 소개하거나 답하지 마 — 네 얘기만 해. {MAC_BOT_NAME}은 "
                "같은 요청에 별도로, 독립적으로 응답해.]\n" + prompt_text
            )

        existing_thread_id = _load_codex_chat_thread_id(alias)
        if existing_thread_id:
            # "--" stops codex's own flag parsing before `prompt_text` —
            # without it, a message starting with "-" (e.g. a stray
            # "--dangerously-bypass-approvals-and-sandbox") would be
            # parsed as a codex CLI option instead of prompt text
            # (confirmed live).
            #
            # --skip-git-repo-check 추가(2026-07-30, 실채널 테스트로 발견한
            # 실제 버그): `codex exec resume`엔 -C/--cd 플래그가 아예 없다
            # (--help로 확인) — 그런데 신뢰 검사는 여전히 이 프로세스의
            # 실제 OS cwd를 본다. codex-bot.py는 launchd plist에
            # WorkingDirectory가 지정 안 돼 있어 실제 cwd가 `/`다(lsof로
            # 확인) — `/`는 신뢰된 디렉토리가 아니므로 resume 턴마다
            # "Not inside a trusted directory..." 에러가 났다. 새 스레드
            # 경로(-C <trusted dir> 명시)는 우연히 안 걸렸던 것뿐 — 재현:
            # `cd / && codex exec resume <thread_id> ...`로 100% 재현,
            # --skip-git-repo-check 추가로 해결 확인. 이 버그는 오늘 세션
            # 이전부터 있던 것으로, 실제 채널 히스토리에 동일 에러가 과거에도
            # 반복 기록돼 있었다(사용자가 매번 `!코덱스대화초기화`로 우회).
            # resume은 이미 확립된 스레드를 이어가는 것뿐이라 git 저장소
            # 경계를 새로 검증할 필요가 없다는 점에서 안전한 우회.
            args = [str(PROVIDER_SANDBOX), str(CODEX_BIN), "exec", "resume", existing_thread_id, "--skip-git-repo-check", *CODEX_CHAT_REASONING_EFFORT_ARGS, "--json", "--", prompt_text]
            # Previously only sent on a brand-new thread (else branch
            # below) — a resume turn computed dirty_note above but threw
            # it away, so the "uncommitted changes already present"
            # warning silently disappeared after the first turn of a
            # conversation.
            if dirty_note:
                await message.channel.send(dirty_note.lstrip("\n"))
        else:
            # 2026-07-30, 사용자 명시적 요청: 정체성(CODEX_BOT_PERSONA)은
            # 새 스레드 시작 시점에만 넣는다 — 코덱스엔 claude -p의
            # --append-system-prompt 같은 "대화 밖에서 매턴 재주입되는"
            # 시스템프롬프트 전용 플래그가 없어서, 이 텍스트는 실제로
            # prompt_text의 일부로 코덱스 자신의 스레드 히스토리에 그대로
            # 남는다 — 매턴 반복하면 resume될 때마다 같은 자기소개가 계속
            # 쌓여서 노이즈가 된다. 새 스레드 첫 턴에만 넣고, 그 뒤로는
            # exec resume이 이어받는 스레드 자체 기억에 맡긴다.
            new_thread_prompt_text = f"{CODEX_BOT_PERSONA}\n\n{prompt_text}"
            args = [str(PROVIDER_SANDBOX), str(CODEX_BIN), "exec", *CODEX_CHAT_REASONING_EFFORT_ARGS, "--json", "-s", "workspace-write", "-C", str(cwd), "--", new_thread_prompt_text]
            await message.channel.send(f"`{alias}` 코덱스 대화를 새로 시작합니다.{dirty_note}")

        proc = await asyncio.create_subprocess_exec(
            *args, env=SUBPROCESS_ENV,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        CODEX_CURRENT_PROCS[str(cwd.resolve())] = proc
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=CODEX_CHAT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await _kill_process_group_graceful(proc)
            # On a resumed turn, a timeout/failure never overwrites the
            # saved thread_id (only a SUCCESSFUL run does, below), so a
            # broken resume just fails the same way forever without this
            # hint.
            reset_hint = _codex_chat_reset_hint(alias, existing_thread_id)
            # 2026-07-29 fix: show the partial diff up to the kill point
            # instead of only "직접 확인해주세요" with zero information —
            # `before` was already captured above, same as !코덱스's
            # handle_codex_dispatch (which got this same fix).
            after = await _dirty_snapshot(cwd)
            diff_stat = await _diff_summary(cwd, before, after)
            timeout_msg = f"⚠️ 응답이 {CODEX_CHAT_TIMEOUT_SECONDS // 60}분을 넘어서 강제 종료했습니다."
            if diff_stat:
                timeout_msg += f" 강제종료 직전까지 실제 변경된 파일(중간에 끊긴 상태일 수 있음):\n```\n{diff_stat}\n```"
            else:
                timeout_msg += f" 강제종료 시점까지 감지된 파일 변경은 없습니다 — {alias} 저장소를 직접 확인해주세요."
            timeout_msg += reset_hint
            await message.channel.send(timeout_msg[:1900])
            return

        raw = (stdout or b"").decode(errors="replace")
        thread_id, reply_text = _parse_codex_json_events(raw)

        if proc.returncode != 0 or thread_id is None:
            # 2026-07-30, 사용자 명시적 요청("실제 계정을 따라가지 말고
            # 멀티에이전트를 따라가야 한다") — discord-bot.py의
            # 자유채팅의 provider 폴백과 대칭: 콕스가 계정/사용량 한도로 실패하면
            # "재시도하세요"로 끝내지 말고 맥으로 자동 폴백한다.
            # _delegate_to_claude()는 콕스→맥 위임 마커 처리에 이미 쓰던
            # 헬퍼를 그대로 재사용 — "판단해서 위임"이든 "실패해서 폴백"이든
            # 맥에게 결국 필요한 건 "이 요청에 최선을 다해 답하라"는 같은
            # 지시라 새 함수를 만들 필요가 없다.
            if QUOTA_LIMIT_PATTERN.search(raw):
                await message.channel.send(f"⏳ 콕스(코덱스)가 계정 사용 한도로 응답하지 못했습니다 — 맥으로 자동 전환합니다.\n```\n{raw[-300:]}\n```")
                await _delegate_to_claude(message, text)
                return
            reset_hint = _codex_chat_reset_hint(alias, existing_thread_id)
            await message.channel.send(f"❌ 코덱스 실행 실패 (exit={proc.returncode}).{reset_hint}\n```\n{raw[-1500:]}\n```"[:1900])
            return

        # Only persist AFTER a successful run: an id from a run that
        # failed to even start properly shouldn't become what the next
        # message tries to resume.
        _save_codex_chat_thread_id(alias, thread_id)

        # 2026-07-30, 콕스→맥 위임(사용자 요청 "콕스야도 똑같이 위임 판단하게
        # 해줘"): 코덱스가 CODEX_DELEGATE_TO_MAC_MARKER로 시작하는 응답을
        # 내면, diff 리포트를 건너뛰고(위임 판단이면 파일 변경이 없다고
        # 가정) _delegate_to_claude()가 대신 처리한다.
        stripped_reply = (reply_text or "").strip()
        if stripped_reply.startswith(CODEX_DELEGATE_TO_MAC_MARKER):
            delegated_task = stripped_reply[len(CODEX_DELEGATE_TO_MAC_MARKER):].strip()
            if delegated_task:
                await _delegate_to_claude(message, delegated_task)
                return
            # 마커만 있고 실제 위임할 내용이 없으면(코덱스가 형식을 잘못
            # 따름) 안전하게 아래 원래 응답 경로로 폴백.

        # Never trust Codex's own conversational reply as proof of what
        # changed — same before/after diff verification as !코덱스.
        after = await _dirty_snapshot(cwd)
        diff_stat = await _diff_summary(cwd, before, after)

        lines = [reply_text or "(응답 없음)"]
        if diff_stat:
            lines.append(f"\n변경된 파일:\n```\n{diff_stat}\n```")
        await message.channel.send("\n".join(lines)[:1900])
    except Exception as e:
        await message.channel.send(f"❌ 코덱스대화 실행 중 예외: {e}")
    finally:
        CODEX_CURRENT_PROCS.pop(str(cwd.resolve()), None)


async def _delegate_to_claude(message: discord.Message, task: str) -> None:
    """콕스가 CODEX_DELEGATE_TO_MAC_MARKER로 위임을 신호하면, 코덱스 자신의
    exec 샌드박스(workspace-write) 안이 아니라 codex-bot.py 자신의 호스트
    레벨 프로세스(샌드박스 밖)에서 claude -p를 직접 실행한다.

    콕스의 샌드박스 안에서 claude -p를 직접 부르면 네트워크 아웃바운드가
    막혀 응답 없음/90초 타임아웃이 나는 것을 실측 확인했다(2026-07-30,
    scratch 저장소에서 `codex exec -s workspace-write`로 직접 재현) —
    `-s danger-full-access`로 풀면 되겠지만 파일쓰기 범위 제한이라는 원래
    안전장치를 없애는 거라 채택 안 함. 그래서 위임 자체는 코덱스가 "판단"만
    하고(마커로 신호), 실제 네트워크 호출은 이 파이썬 함수가 대신 한다.

    맥의 영구 자유채팅 세션(discord-bot.py의 FREE_CHAT_SESSION_FILE)은
    재사용하지 않고 매번 새 1회성 대화로 처리한다 — 두 봇 프로세스가 같은
    세션 파일에 동시에 --resume을 시도하는 크로스프로세스 경쟁을 피하기
    위함(!코덱스가 콕스 자신의 영구 스레드를 안 쓰고 매번 1회성인 것과
    같은 이유 — discord-bot.py는 이 함수의 존재 자체를 모르므로 파일 락
    같은 조율 장치도 못 만들었을 것).
    """
    skip_reason = await usage_gate_check("claude")
    if skip_reason:
        await message.channel.send(f"⏳ 맥에게 위임을 건너뜁니다 — 계정 사용량 부족.\n{skip_reason}\n사용량 회복 후 다시 시도해주세요.")
        return

    cross_context = await fetch_cross_bot_context(message.channel, client.user.id)
    if cross_context:
        prompt_text = (
            "[참고 — 같은 Discord 채널에서 최근 다른 봇(콕스)과 나눈 대화. "
            "네 실제 세션 기록이 아니라 곁눈질로 보는 참고자료일 뿐이니, "
            "여기 내용을 사실로 단정하지 말고 필요할 때만 자연스럽게 참고해:]\n"
            f"{cross_context}\n\n[콕스가 위임한 요청]\n{task}"
        )
    else:
        prompt_text = f"[콕스가 위임한 요청]\n{task}"

    try:
        proc = await asyncio.create_subprocess_exec(
            str(PROVIDER_SANDBOX), str(CLAUDE_BIN), "-p", prompt_text, "--output-format", "text",
            "--append-system-prompt", MAC_BOT_PERSONA,
            env=SUBPROCESS_ENV,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_DELEGATE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await _kill_process_group_graceful(proc)
            await message.channel.send(f"⚠️ 맥에게 위임한 작업이 {CLAUDE_DELEGATE_TIMEOUT_SECONDS // 60}분을 넘어서 강제 종료했습니다 — 직접 확인이 필요합니다.")
            return
        out_text = (stdout or b"").decode(errors="replace").strip()
        await message.channel.send(f"🔀 맥에게 위임:\n{out_text}"[:1900] if out_text else "🔀 맥에게 위임했지만 응답이 없습니다.")
    except Exception as e:
        await message.channel.send(f"❌ 맥에게 위임 중 예외: {e}")


async def handle_codex_stop(message: discord.Message):
    """!코덱스중지 <별칭> — kills the in-flight !코덱스/!코덱스대화 run for
    that repo, if any. 기능 패리티 갭 해소(2026-07-30, 사용자 확정) —
    discord-bot.py의 handle_free_chat_stop과 동일한 목적이지만, 이쪽은 여러
    별칭이 독립적으로 동시에 돌 수 있어 단일 전역 대신 별칭(정확히는
    정규화된 cwd)별로 대상을 지정해야 한다.

    discord-bot.py의 !중지와 같은 이유로 CODEX_DISPATCH_LOCKS도 함께
    확인한다 — 락은 잡았지만 실제 subprocess가 아직 안 뜬 짧은 창(프리플라이트
    게이트 대기 중 등)에는 CODEX_CURRENT_PROCS에 아직 없어서, 그 상태를
    "실행 중인 게 없다"로 잘못 알리지 않기 위함.
    """
    if str(message.author.id) != FREE_CHAT_USER_ID:
        return

    parts = message.content.strip().split(maxsplit=1)
    aliases = ", ".join(sorted(CODEX_REPO_ALIASES))
    if len(parts) < 2:
        await message.channel.send(f"사용법: `!코덱스중지 <저장소별칭>`\n사용 가능한 별칭: {aliases}")
        return

    alias = parts[1].strip()
    cwd = CODEX_REPO_ALIASES.get(alias)
    if cwd is None:
        await message.channel.send(f"알 수 없는 저장소 별칭: `{alias}`\n사용 가능한 별칭: {aliases}")
        return

    lock_key = str(cwd.resolve())
    proc = CODEX_CURRENT_PROCS.get(lock_key)
    lock = CODEX_DISPATCH_LOCKS.get(lock_key)
    try:
        if proc is not None:
            # graceful kill — 이 프로세스는 workspace-write 코덱스 실행이라
            # mid-write SIGKILL 손상 위험이 동일하게 적용됨.
            await _kill_process_group_graceful(proc)
            await message.channel.send(f"`{alias}`에 대한 중단 요청을 보냈습니다.")
        elif lock is not None and lock.locked():
            await message.channel.send(f"`{alias}` 실행을 준비 중입니다 — 아직 중단할 프로세스가 없습니다, 잠시 후 다시 시도해주세요.")
        else:
            await message.channel.send(f"`{alias}`에 대해 지금 실행 중인 게 없습니다.")
    except Exception as e:
        await message.channel.send(f"❌ 중단 처리 중 예외: {e}")


@client.event
async def on_ready():
    print(f"logged in as {client.user} (id={client.user.id})")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if str(message.channel.id) != AUTHORIZED_CHANNEL_ID:
        return

    content = message.content.strip()
    # "!코덱스대화초기화"/"!코덱스중지"는 더 일반적인 "!코덱스대화"/"!코덱스"보다
    # 먼저 체크해야 한다 — 넷 다 "!코덱스" 접두어를 공유하고, Python의 elif
    # 체인은 첫 매치를 취하므로 순서가 로직 그 자체다(discord-bot.py에서 이
    # 라우팅이 처음 나왔을 때 겪은 것과 같은 함정).
    if content.startswith("!코덱스대화초기화"):
        await handle_codex_chat_reset(message)
    elif content.startswith("!코덱스중지"):
        # 2026-07-30 추가(사용자 확정, 기능 패리티 갭 해소): discord-bot.py의
        # !중지에 대응하는 명령이 이쪽엔 없어서, 오래 걸리는 !코덱스/
        # !코덱스대화를 멈추려면 30분 타임아웃을 기다려야 했다.
        await handle_codex_stop(message)
    elif content.startswith("!코덱스대화"):
        await handle_codex_chat(message)
    elif content.startswith("!코덱스"):
        await handle_codex_dispatch(message)
    elif is_codex_wake_word(content):
        await handle_codex_chat_wake(message)
    elif is_group_address(content):
        # 2026-07-30, 사용자 실제 테스트("각자 자기소개 해줘") 발견: 콕스를
        # 이름으로 지목하지 않아도 여러 참가자를 한꺼번에 지칭하는 표현이면
        # 맥과 별개로 콕스도 응답한다 — discord-bot.py는 이 단어들을 배제
        # 조건에 안 넣으므로(맥도 여전히 응답) 결과적으로 단체방처럼 둘 다
        # 답하게 된다.
        await handle_codex_chat_wake(message)


if __name__ == "__main__":
    client.run(CONFIG["token"])
