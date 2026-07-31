#!/usr/bin/env python3
"""Direct Telegram group bridge for Claude, Codex, and Antigravity.

Each launchd instance has one Telegram token and one provider role.  The
bridge never calls OpenClaw: it invokes the provider CLI directly, using the
same workspace and subprocess environment as the Discord adapters.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import os
import re
import signal
import subprocess
import sys
import time
import traceback
import unicodedata
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.constants import ChatType, MessageEntityType
from telegram.error import Conflict, NetworkError, TelegramError
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from edge_agent_locks import canonical_repository_root
from edge_agent_plan_gate import clear_pending, is_approval, load_pending, save_pending
from edge_agent_reflection import write_reflection, write_worktree_metadata
from edge_agent_runtime_adapter import EfficiencyMode, RuntimeEfficiencyAdapter
from edge_agent_session_bridge import start_session, update_session, bounded_context
from edge_agent_skill_connector import build_skill_context
from edge_agent_state import write_task_state
from edge_agent_parallel_locks import repository_lifecycle_lock
from edge_agent_workspace_lock import RepoLockBusy, acquire_repo_lock


HOME = Path.home()
PROVIDER_SANDBOX = Path(__file__).resolve().with_name("edge-agent-provider-sandbox.sh")
WORKSPACE = Path(
    os.environ.get("TELEGRAM_AGENT_WORKSPACE", str(HOME / ".edge-agent-worktrees" / "telegram-bootstrap"))
).expanduser().resolve()
CODEX_WORKSPACE = Path(
    os.environ.get(
        "TELEGRAM_AGENT_CODEX_WORKSPACE",
        str(HOME / ".edge-agent-worktrees" / "telegram-bootstrap"),
    )
).expanduser().resolve()
CODEX_SOURCE_REPO = Path(
    os.environ.get("TELEGRAM_AGENT_SOURCE_REPO", str(HOME / "mac-agent"))
).expanduser().resolve()
CODEX_TASK_WORKTREE_ROOT = Path(
    os.environ.get(
        "TELEGRAM_CODEX_TASK_WORKTREE_ROOT",
        str(HOME / ".edge-agent-worktrees" / "telegram-tasks"),
    )
).expanduser().resolve()
RUNTIME_CONTRACT = Path(
    os.environ.get("EDGE_AGENT_RUNTIME_CONTRACT", str(HOME / ".edge-agent" / "EDGE_AGENT.md"))
).expanduser().resolve()
ACTIVE_TASK_WORKSPACE: Path | None = None
ACTIVE_LOGICAL_SESSION_ID: str | None = None
ROLE = os.environ.get("TELEGRAM_AGENT_ROLE", "").strip().lower()
TOKEN_FILE = Path(
    os.environ.get(
        "TELEGRAM_AGENT_TOKEN_FILE",
        str(HOME / ".config" / "agent-telegram" / f"{ROLE}.token"),
    )
).expanduser()
# GROUP_TITLE is no longer used for access control (see addressed_text) —
# kept only as a legacy env var name some old scripts/docs may still set;
# harmless if present, unused if not.
GROUP_TITLE = os.environ.get("TELEGRAM_AGENT_GROUP_TITLE", "edgeAI-agent")
# The numeric chat id is the ONLY identity check — a group *title* is
# trivially spoofable (anyone can name a new group "edgeAI-agent"), so it
# must never be sufficient on its own. Required, not optional: fail closed
# at startup rather than silently falling back to title-only matching if
# it's ever missing from the environment (round-7 independent review found
# the old fallback-with-a-log-warning behavior was itself the fail-open
# gap — a missing env var should be loud, not a quiet security downgrade).
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_AGENT_CHAT_ID", "").strip()
if not ALLOWED_CHAT_ID:
    raise SystemExit(
        "TELEGRAM_AGENT_CHAT_ID is required (set it to this group's numeric "
        "chat_id, visible in this bot's own log after it first sees a "
        "message in the group) — refusing to start with title-only auth."
    )
TIMEOUT_SECONDS = int(os.environ.get("TELEGRAM_AGENT_TIMEOUT_SECONDS", "1800"))
STALE_SECONDS = int(os.environ.get("TELEGRAM_AGENT_STALE_SECONDS", "600"))
# Claude/Antigravity verify calls only have to read a diff and render a
# verdict — not author code — so they get their own, much shorter timeout
# than a full codex authoring run. Round-7 independent review (Codex +
# Antigravity, 2026-07-31) flagged the shared 1800s timeout as enabling a
# ~3h worst-case _BUSY_LOCK hold across 2 rounds; both agents independently
# recommended cutting verify-call time specifically rather than the
# authoring timeout (codex genuinely may need the full budget to code).
CODEX_VERIFY_CALL_TIMEOUT_SECONDS = int(os.environ.get("TELEGRAM_AGENT_CODEX_VERIFY_CALL_TIMEOUT_SECONDS", "600"))
CHUNK_SIZE = 3900
MAX_CHUNKS = int(os.environ.get("TELEGRAM_AGENT_MAX_CHUNKS", "15"))

# Single source of truth for the three roles — binary, display label,
# BotFather username, and wake words all live together so adding a role can't
# silently update one dict and forget another.
ROLES = {
    "claude": {
        "binary": HOME / ".local" / "bin" / "claude",
        "label": "Claude",
        "username": "edgeai_stk_bot",
        "wake_words": ("클로드",),
    },
    "codex": {
        "binary": Path("/opt/homebrew/bin/codex"),
        "label": "Codex",
        "username": "edgeai_macmini_bot",
        "wake_words": ("코덱스", "콕스"),
    },
    "antigravity": {
        "binary": HOME / ".local" / "bin" / "agy",
        "label": "Antigravity",
        "username": "edgeai_anti_bot",
        "wake_words": ("안티", "안티그래비티"),
    },
}

if ROLE not in ROLES:
    raise SystemExit(f"TELEGRAM_AGENT_ROLE must be one of {sorted(ROLES)}")

CLI = ROLES[ROLE]["binary"]
ROLE_LABELS = {role: cfg["label"] for role, cfg in ROLES.items()}
ROLE_USERNAMES = {role: cfg["username"] for role, cfg in ROLES.items()}
ROLE_WAKE_WORDS = {role: cfg["wake_words"] for role, cfg in ROLES.items()}

# Video-derived efficiency policies are deliberately opt-in.  The default
# keeps the long-tested Telegram command line byte-for-byte equivalent; the
# pilot can enable bounded prompt/profile behavior without changing launchd
# or the provider credentials.
_EFFICIENCY_ADAPTER = RuntimeEfficiencyAdapter()

if not TOKEN_FILE.exists():
    raise SystemExit(f"Telegram token file not found: {TOKEN_FILE}")
_token_mode = TOKEN_FILE.stat().st_mode & 0o777
if _token_mode & 0o077:
    # Fail closed rather than just logging: a token readable by group/other
    # is a live secret-exposure bug, not a warning-worthy code-quality nit —
    # any other local account could read it and impersonate this bot.
    raise SystemExit(
        f"Telegram token file has unsafe permissions {oct(_token_mode)} (need 0600 or stricter): {TOKEN_FILE}"
    )
TOKEN = TOKEN_FILE.read_text(encoding="utf-8").strip()
if not TOKEN:
    raise SystemExit(f"Telegram token file is empty: {TOKEN_FILE}")

ENV = {
    **os.environ,
    "HOME": str(HOME),
    "PATH": f"/opt/homebrew/bin:{HOME / '.local' / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
}


def log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{timestamp}] [{ROLE}] {message}", file=sys.stderr, flush=True)


def _provider_workspace(role: str) -> Path:
    if ACTIVE_TASK_WORKSPACE is not None:
        return ACTIVE_TASK_WORKSPACE
    return CODEX_WORKSPACE if role == "codex" else WORKSPACE


def _create_task_worktree(task_id: str) -> Path:
    CODEX_TASK_WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    target = CODEX_TASK_WORKTREE_ROOT / task_id
    if target.exists():
        return target
    if not (CODEX_SOURCE_REPO / ".git").exists():
        raise RuntimeError(f"Telegram 기준 저장소가 없습니다: {CODEX_SOURCE_REPO}")
    # Use the same repository lifecycle lock as the provider-neutral
    # WorktreeManager. Telegram retains its compatibility worktree policy,
    # but its git worktree mutation must not race terminal/parallel creation.
    with repository_lifecycle_lock(CODEX_SOURCE_REPO):
        if target.exists():
            return target
        completed = subprocess.run(
            ["/usr/bin/git", "-C", str(CODEX_SOURCE_REPO), "worktree", "add", "--detach", str(target), "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git worktree add 실패").strip()
        raise RuntimeError(f"Telegram 작업 worktree 생성 실패: {detail[-500:]}")
    return target


def _auth_source(role: str) -> str:
    if role == "claude":
        return str(HOME / ".claude")
    if role == "codex":
        return os.environ.get("CODEX_HOME", str(HOME / ".codex"))
    return os.environ.get("GEMINI_DIR", str(HOME / ".gemini"))


def _runtime_prompt_parts(prompt: str) -> tuple[str, dict[str, str | int]]:
    skill_context = build_skill_context(prompt)
    skill_block = f"\n{skill_context}\n" if skill_context else ""
    session_block = ""
    if ACTIVE_LOGICAL_SESSION_ID:
        try:
            session_block = f"\n{bounded_context(ACTIVE_LOGICAL_SESSION_ID)}\n"
        except (FileNotFoundError, ValueError) as exc:
            log(f"공유 세션 컨텍스트 로드 실패(계속 진행): {exc}")
    context = (
        f"공통 운영 계약을 먼저 읽어라: {RUNTIME_CONTRACT}. "
        "계약은 권한 부여가 아니며, 실제 실행 결과와 현재 작업공간을 확인하라.\n\n"
        f"{skill_block}"
        f"{session_block}"
    )
    if _EFFICIENCY_ADAPTER.mode == EfficiencyMode.ENFORCE:
        prepared = _EFFICIENCY_ADAPTER.prepare(
            prompt,
            provider=ROLE,
            context=context,
            # Existing portable skill connector remains the source of skill
            # text for this bot; the new policy controls only bounded input.
            skill_documents={},
        )
        return prepared.prompt, prepared.cli_options()
    return f"{context}[사용자 요청]\n{prompt}", {}


def _runtime_prompt(prompt: str) -> str:
    return _runtime_prompt_parts(prompt)[0]


def _changed_file_count(workspace: Path | None) -> int:
    """Return a bounded count for evidence only; never stores file names."""
    if workspace is None or not (workspace / ".git").exists():
        return 0
    try:
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(workspace), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return sum(1 for line in (result.stdout or "").splitlines() if line.strip())


def _record_telegram_efficiency(
    *,
    task_id: str,
    prompt: str,
    status: str,
    output: str = "",
    started: float,
    workspace: Path | None,
) -> None:
    """Best-effort task evidence; recording failure never changes work status."""
    if _EFFICIENCY_ADAPTER.mode == EfficiencyMode.OFF:
        return
    try:
        prepared = _EFFICIENCY_ADAPTER.prepare(prompt, provider=ROLE)
        _EFFICIENCY_ADAPTER.record(
            prepared,
            task_id=task_id,
            step_id="telegram-task",
            status=status,
            output=output,
            changed_files=_changed_file_count(workspace),
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            verification_tier="telegram-task",
        )
    except Exception as exc:
        log(f"효율성 원장 기록 실패(작업 결과에는 영향 없음): {type(exc).__name__}")


def _harden_log_permissions() -> None:
    # launchd creates StandardOutPath/StandardErrorPath with the process's
    # default umask (often 0644 — world-readable). Provider stderr can
    # contain workspace paths and task content, so lock it down regardless
    # of which log file launchd pointed us at.
    #
    # Must run before the FIRST log() call (see main()), not after — the
    # original ordering logged "Starting direct Telegram ... bot" before
    # calling this, so that one line (plus whatever launchd/the OS wrote to
    # the file before us) was exposed at the default mode for however long
    # it took this call to run. A silent OSError also used to mean "log
    # stays world-readable forever, nobody finds out" — now logged instead
    # so it's at least visible in the (now-secured-if-possible) log itself.
    for fd in (1, 2):
        try:
            os.fchmod(fd, 0o600)
        except OSError as exc:
            log(f"경고: fd={fd} 로그 권한 강화 실패 ({exc}) — 로그 파일이 기본 권한(대개 world-readable)으로 남을 수 있음.")


# Longest-first: _strip_wake_particle does a first-match endswith loop, so a
# 2-character particle must be checked before any 1-character particle that
# could be its suffix (e.g. "에게" before a bare "게", if one existed).
# Includes vocative particles (야/아/씨/님) and common grammatical case
# particles (가/는/를/을/은/이/에게/한테) — round-5 independent review found
# "코덱스가 이 파일을 봐줘" / "안티는 어떻게 생각해?" weren't recognized as
# addressing that bot because only vocative forms were stripped.
_WAKE_PARTICLE_SUFFIXES = ("에게", "한테", "야", "아", "씨", "님", "가", "는", "를", "을", "은", "이")


def _strip_wake_punct(token: str) -> str:
    # Category-based instead of a fixed character list so trailing emoji
    # ("코덱스🙂"), colons ("코덱스:"), and quotes all get stripped without
    # needing to keep enumerating characters by hand.
    while token and unicodedata.category(token[-1])[0] in ("P", "S"):
        token = token[:-1]
    return token


def _strip_wake_particle(token: str) -> str:
    for particle in _WAKE_PARTICLE_SUFFIXES:
        if token.endswith(particle) and token != particle:
            return token[: -len(particle)]
    return token


def _wake_roles(text: str) -> set[str]:
    # Only the first/last token is checked (not a bare substring search) so a
    # message merely mentioning a bot mid-sentence ("어제 코덱스가 이상했어")
    # doesn't get misrouted as an address to it.
    tokens = text.strip().split()
    if not tokens:
        return set()
    first = _strip_wake_particle(_strip_wake_punct(tokens[0]))
    last = _strip_wake_particle(_strip_wake_punct(tokens[-1]))
    return {
        role for role, words in ROLE_WAKE_WORDS.items()
        if first in words or last in words
    }


def _entity_substring(entity, text: str) -> str:
    # entity.offset/.length are UTF-16 code-unit counts (Telegram's own
    # indexing unit), NOT Python codepoint indices — they only coincide for
    # text with no characters outside the BMP. A message like "🙂 @bot_name"
    # has the emoji as a surrogate pair (2 UTF-16 units, 1 Python codepoint),
    # which would shift every entity after it and silently break
    # mention/command matching. Round-trip through UTF-16 to slice correctly.
    encoded = text.encode("utf-16-le")
    start = entity.offset * 2
    end = start + entity.length * 2
    return encoded[start:end].decode("utf-16-le")


def _mentioned_roles_from_entities(text: str, entities) -> set[str]:
    # Telegram-parsed entities are exact — "@edgeai_macmini_bot_extra" is a
    # different (non-)account and Telegram won't tag it as a mention of ours,
    # unlike a bare `"@edgeai_macmini_bot" in text` substring check.
    username_to_role = {username.lower(): role for role, username in ROLE_USERNAMES.items()}
    roles: set[str] = set()
    for entity in entities or []:
        if entity.type == MessageEntityType.MENTION:
            role = username_to_role.get(_entity_substring(entity, text).lstrip("@").lower())
            if role:
                roles.add(role)
        elif entity.type == MessageEntityType.BOT_COMMAND:
            match = re.match(r"^/(\w+)(?:@(\S+))?$", _entity_substring(entity, text), re.IGNORECASE)
            if not match:
                continue
            cmd_role, target_username = match.group(1).lower(), match.group(2)
            if target_username:
                # Explicit "@bot" suffix is authoritative — "/claude@codex_bot"
                # is addressed to whichever bot that username belongs to, not
                # to claude just because the command word says so. If the
                # suffix doesn't match any of our three bots at all (e.g. a
                # typo, or a genuinely unrelated bot username), do NOT fall
                # back to cmd_role — that would make "/claude@some_other_bot"
                # answer as claude despite being explicitly addressed
                # elsewhere. add_unmatched_marker forces the "silent" branch
                # below by adding a role name that can never equal ROLE.
                target_role = username_to_role.get(target_username.lower())
                roles.add(target_role if target_role else "__unmatched_suffix__")
            elif cmd_role in ROLE_USERNAMES:
                roles.add(cmd_role)
            else:
                # A real slash command that just isn't one of ours
                # (/start, /help, Telegram's own /settings, etc.) — MUST
                # NOT be treated as "no one addressed, free chat for all
                # three." Round-6 independent review caught this live: an
                # empty `roles` here falls straight through to the
                # unaddressed-plain-message branch, so any unrecognized
                # slash command made all three bots fire their LLM CLI at
                # once. The sentinel forces every role's own addressed_text
                # to see itself excluded, i.e. all three go silent.
                roles.add("__unmatched_suffix__")
    return roles


def _mentioned_roles_from_regex(text: str) -> set[str]:
    # Fallback only, for the rare message with no entities at all. Plain
    # substring/regex matching, same as the entity-based path minus the
    # precision Telegram's own tokenizer gives us.
    lowered = text.lower()
    roles: set[str] = set()
    for role, username in ROLE_USERNAMES.items():
        # Word-boundary check so "@edgeai_macmini_bot_extra" (a different,
        # longer username) doesn't register as a mention of "@edgeai_macmini_bot".
        pattern = re.compile(rf"@{re.escape(username.lower())}(?![a-z0-9_])")
        if pattern.search(lowered):
            roles.add(role)
    match = re.match(r"^/(\w+)(?:@(\S+))?(?:\s|$)", text, re.IGNORECASE)
    if match:
        cmd_role, target_username = match.group(1).lower(), match.group(2)
        if target_username:
            username_to_role = {u.lower(): r for r, u in ROLE_USERNAMES.items()}
            target_role = username_to_role.get(target_username.lower())
            roles.add(target_role if target_role else "__unmatched_suffix__")
        elif cmd_role in ROLE_USERNAMES:
            roles.add(cmd_role)
        else:
            # Same fix as the entity-based path above: an unrecognized slash
            # command (/start, /help, ...) must silence all three, not be
            # treated as an unaddressed free-chat message.
            roles.add("__unmatched_suffix__")
    return roles


def _is_stale(message) -> bool:
    if not message.date:
        return False
    age = (datetime.now(timezone.utc) - message.date).total_seconds()
    return age > STALE_SECONDS


# Two things independent reviews (round 5, 2026-07-31) flagged as security
# gaps that are deliberately NOT fixed here — both are the user's own
# explicit requirement from earlier in this same session, not oversights:
#   1. No per-sender allowlist inside the group: "단체방 전체 누구나" (anyone
#      in the group, not just the owner) was the explicit answer when asked
#      whether to restrict plain-chat answering to the owner only. Adding a
#      TELEGRAM_AGENT_ALLOWED_USER_IDS gate now would silently override that
#      decision. TELEGRAM_AGENT_CHAT_ID (added this session) is the intended
#      boundary — trust is "whoever is in this specific group," not
#      "whoever sent this specific message."
#   2. An unaddressed plain message still runs all three providers
#      sequentially against the shared WORKSPACE (each may see the previous
#      one's edits) rather than picking a single writer — that's the direct
#      consequence of "all three should answer free chat," the explicit
#      feature this session was asked to build in the first place.
# If either default should change, that's a product decision for the user
# to make, not something to silently harden away during a review pass.
def addressed_text(update: Update) -> str | None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return None
    # ALLOWED_CHAT_ID (numeric, unspoofable) is the sole identity check —
    # required at startup (see module-level check below), not just an
    # optional hardening layer. GROUP_TITLE is intentionally NOT checked
    # here anymore: keeping it as a second required condition meant renaming
    # the real group (e.g. "edgeAI-agent" -> "edgeAI-agent-v2") silently
    # broke all three bots with no error, even though the chat_id — the
    # thing that actually identifies the group — never changed. Caught in
    # round-7 independent review, 2026-07-31.
    if str(chat.id) != ALLOWED_CHAT_ID:
        return None
    if message.from_user and message.from_user.is_bot:
        # Discord-style free chat: every plain message from a human gets a
        # reply, but the three role bots share this group, so without this
        # each bot's reply would re-trigger the other two — infinite loop.
        return None
    text = message.text or message.caption or ""
    if not text:
        return None

    raw_entities = message.entities or message.caption_entities or []
    if raw_entities:
        mentioned_roles = _mentioned_roles_from_entities(text, raw_entities)
    else:
        mentioned_roles = _mentioned_roles_from_regex(text)
    mentioned_roles |= _wake_roles(text)

    if mentioned_roles and ROLE not in mentioned_roles:
        # Explicitly addressed to (a) different role bot(s) — stay silent.
        return None

    # Strip every registered bot's @mention, not just this one's own — a
    # message can name several bots at once ("@claude_bot @codex_bot 안녕"),
    # and whichever one(s) aren't ROLE would otherwise leak their raw
    # "@other_bot_username" tag straight into the prompt sent to the LLM.
    # Caught live in round-6 independent review, 2026-07-31.
    #
    # Boundary-checked (negative lookahead for a username character), NOT a
    # bare re.sub: without it, a plain-text message containing
    # "@edgeai_macmini_bot_extra" (a longer, different string — not our
    # bot) would have its "@edgeai_macmini_bot" prefix silently cut out
    # from unrelated text, since that substring genuinely does appear
    # inside it. Caught in round-7 independent review, 2026-07-31.
    for username in ROLE_USERNAMES.values():
        pattern = re.compile(rf"@{re.escape(username)}(?![a-zA-Z0-9_])", re.IGNORECASE)
        text = pattern.sub("", text)
    # Role-agnostic on purpose: "/claude@edgeai_macmini_bot 작업" routes to
    # codex (the @suffix overrides the command word — see
    # _mentioned_roles_from_entities above), so a pattern anchored to only
    # this role's own command word wouldn't match and the raw "/claude@..."
    # prefix would leak into the prompt sent to the LLM. By the time we're
    # here, mentioned_roles has already confirmed this message is meant for
    # ROLE regardless of which command word it used, so stripping any
    # leading "/word(@target)?" is correct.
    command = re.compile(r"^/\w+(?:@\S+)?(?:\s|$)", re.IGNORECASE)
    text = command.sub("", text, count=1).strip()
    return text or "간단히 자기소개하고, 내가 어떤 일을 맡기면 되는지 알려줘."


async def _terminate_process_group(proc: asyncio.subprocess.Process, pgid: int, grace_seconds: float = 5.0) -> None:
    # proc.kill() only signals the direct child. Provider CLIs can spawn
    # their own subprocesses (tool calls, shells) which start_new_session's
    # own process group covers — os.killpg is what actually reaches them.
    #
    # `pgid` must be captured by the caller right after spawn (see
    # run_provider), NOT re-derived here from proc.pid: if the leader has
    # already exited by the time a timeout fires (its own child/grandchild
    # still running past it), os.getpgid(proc.pid) raises ProcessLookupError
    # and cleanup would silently no-op — caught in round-3 independent
    # review, 2026-07-31.
    #
    # Mirrors discord_bot_common._kill_process_group_graceful, including a
    # bug it already hit and fixed (2026-07-30 Codex review there): watching
    # only proc.wait() treats "the group leader exited" as "the group is
    # done," but a grandchild (e.g. a tool subprocess the leader spawned)
    # can outlive it and never get the SIGKILL escalation. Fix is the same
    # here — after the leader is gone, separately probe the ORIGINAL pgid
    # with a signal-0 existence check before declaring the group empty.
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        await _force_kill_pgid(pgid, proc)
        return
    try:
        os.killpg(pgid, 0)  # existence probe only — signal 0 never actually kills
    except (ProcessLookupError, PermissionError, OSError):
        return  # group confirmed empty (or we can't check further)
    await _force_kill_pgid(pgid, proc)


async def _force_kill_pgid(pgid: int, proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        return
    # SIGKILL alone doesn't reap — the direct child stays a zombie (pipe
    # fds held open, PID not freed) until something awaits it. Bounded so a
    # process wedged even against SIGKILL (e.g. stuck in uninterruptible
    # disk I/O) can't hang this coroutine forever.
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        pass


# Cross-process (not just cross-coroutine) lock so claude/codex/antigravity —
# three separate OS processes, each with full write access to the same
# shared WORKSPACE — can't run a provider CLI concurrently and step on each
# other's file writes. This is a real, reachable case: an unaddressed plain
# message makes all three answer at once (see addressed_text), each
# launching its own workspace-write-capable CLI. Same lock-file *scheme* as
# discord_bot_common.try_acquire_repo_lock() (fcntl.flock on a hashed-path
# lock file) — copied locally rather than imported to avoid this bot
# depending on the discord.py package, BUT deliberately NOT the same
# reject-immediately policy: Discord's callers are guarding against an
# accidental double-fire of what's conceptually one request, where an
# instant "busy" is correct. Here, a plain group message deliberately
# addresses all three bots at once — reject-immediately would mean the 2nd
# and 3rd bot silently never answer the "all three respond" free-chat
# behavior that was the explicit point of building it. So: wait (polling,
# non-blocking flock attempts) up to WORKSPACE_LOCK_WAIT_SECONDS instead of
# failing after the first attempt — correctness (no concurrent writes) is
# kept, "eventually all three answer" is kept, at the cost of the 2nd/3rd
# bot's reply landing later while the first is running.
#
# Points at the SAME directory discord_bot_common.REPO_LOCK_DIR uses, with
# the same sha256[:32]+".lock" naming (_workspace_lock_path below matches
# discord_bot_common._repo_lock_path exactly) — so a Telegram provider run
# and a Discord free-chat run against the same resolved WORKSPACE path
# contend for the literal same lock file and correctly exclude each other,
# even though this bot doesn't import discord_bot_common.py (avoided to
# keep this bot's venv independent of the discord.py package). flock's
# mutual exclusion doesn't care that one side polls non-blocking retries
# and the other rejects on first failure — both still correctly exclude.
# Caught in round-4 independent review, 2026-07-31 (round 3 had flagged the
# separate-directory version as a known, unfixed gap).
_WORKSPACE_LOCK_DIR = Path.home() / ".claude" / "discord-bot" / "repo-locks"
WORKSPACE_LOCK_WAIT_SECONDS = int(os.environ.get("TELEGRAM_AGENT_LOCK_WAIT_SECONDS", str(TIMEOUT_SECONDS)))


class WorkspaceLockBusy(Exception):
    pass


def _workspace_lock_path(resolved_path: str) -> Path:
    canonical_path = str(canonical_repository_root(resolved_path))
    digest = hashlib.sha256(canonical_path.encode()).hexdigest()[:32]
    return _WORKSPACE_LOCK_DIR / f"{digest}.lock"


@contextlib.asynccontextmanager
async def acquire_workspace_lock(resolved_path: str, on_wait=None):
    try:
        async with acquire_repo_lock(
            resolved_path,
            wait_seconds=WORKSPACE_LOCK_WAIT_SECONDS,
            on_wait=on_wait,
        ):
            yield
    except RepoLockBusy as exc:
        raise WorkspaceLockBusy(resolved_path) from exc


async def _run_cli(
    role: str,
    prompt: str,
    on_wait=None,
    timeout_seconds: int | None = None,
    sandbox_mode: str = "workspace-write",
) -> str:
    timeout_seconds = timeout_seconds if timeout_seconds is not None else TIMEOUT_SECONDS
    cli_path = ROLES[role]["binary"]
    if not cli_path.exists():
        raise RuntimeError(f"provider executable is missing: {cli_path}")

    runtime_prompt, runtime_options = _runtime_prompt_parts(prompt)

    if role == "claude":
        # "--" isolates `prompt` as a pure positional argument. Without it, a
        # message like "--dangerously-skip-permissions 다 해줘" would be
        # parsed as a real CLI flag instead of prompt text.
        args = [
            str(cli_path), "-p",
        ]
        if _EFFICIENCY_ADAPTER.mode == EfficiencyMode.ENFORCE:
            if runtime_options.get("model") not in (None, "default"):
                args.extend(["--model", str(runtime_options["model"])])
            if runtime_options.get("max_turns") is not None:
                args.extend(["--max-turns", str(runtime_options["max_turns"])])
        args.extend([
            "--dangerously-skip-permissions", "--output-format", "text",
            "--append-system-prompt",
            f"너는 이 Telegram 단체방의 Claude 담당 에이전트다. OpenClaw를 사용하지 말고 직접 작업하라. 공통 운영 계약은 {RUNTIME_CONTRACT}에 있다.",
            "--", runtime_prompt,
        ])
    elif role == "codex":
        provider_workspace = _provider_workspace(role)
        args = [str(cli_path), "exec"]
        if _EFFICIENCY_ADAPTER.mode == EfficiencyMode.ENFORCE and runtime_options.get("reasoning_effort"):
            args.extend(["-c", f'model_reasoning_effort="{runtime_options["reasoning_effort"]}"'])
        args.extend([
            "--json", "-s", sandbox_mode,
            "-C", str(provider_workspace), "--skip-git-repo-check", "--", runtime_prompt,
        ])
    else:
        provider_workspace = _provider_workspace(role)
        # `--print` is a *string-valued* flag (Go stdlib `flag` package) —
        # its value is whatever argv token comes immediately after it, taken
        # verbatim, dash-prefix or not. That means:
        #   - `--print prompt` is correct: prompt becomes --print's value.
        #   - `--print --output-format text prompt` (the pre-existing code
        #     before this session's fixes) is broken: --print instead
        #     consumes the literal string "--output-format" as its value,
        #     and "text"/the real prompt are silently dropped. Verified live
        #     2026-07-31 — every antigravity Telegram reply before this fix
        #     would have been answering the bot's own "--output-format"
        #     flag name instead of the user's message.
        #   - `--print -- prompt` is ALSO broken the same way: "--" itself
        #     becomes --print's (empty/meaningless) value.
        # So: no --output-format (text is the documented default anyway),
        # no "--", and prompt must be the argv token directly after --print.
        # This also means CLI-flag injection isn't a real risk here the way
        # it is for claude/codex — prompt is consumed whole as --print's
        # value, never re-parsed as a separate flag, regardless of its
        # content.
        # Telegram invokes print mode without an interactive terminal.  In
        # that mode Antigravity cannot present a Bash/tool permission prompt;
        # it soft-denies the tool and may exit 0 with no stdout.  The provider
        # sandbox still blocks writes to Team OS-owned paths, so auto-approve
        # is needed here to make the headless bridge operational while keeping
        # the repository boundary enforced by the wrapper.
        args = [str(cli_path), "--dangerously-skip-permissions", "--print", runtime_prompt]

    if role != "codex":
        provider_workspace = _provider_workspace(role)

    # The wrapper applies the protected Team OS path policy to the provider
    # CLI and every child tool it starts, while preserving the original argv.
    args = [str(PROVIDER_SANDBOX), *args]

    try:
        async with acquire_workspace_lock(str(provider_workspace), on_wait=on_wait):
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(provider_workspace),
                env=ENV,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            # Captured immediately after spawn, not re-derived inside the
            # timeout handler — see _terminate_process_group's docstring.
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = proc.pid
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                await _terminate_process_group(proc, pgid)
                raise RuntimeError(f"{role} 실행 시간이 제한 시간({timeout_seconds}초)을 초과했습니다.")
            except BaseException:
                # Covers asyncio.CancelledError (task cancellation — e.g. app
                # shutdown mid-request; CancelledError is a BaseException,
                # not Exception, so a plain `except Exception` would miss
                # it) and anything else that isn't a plain timeout. Without
                # this, only TimeoutError ever ran cleanup and every other
                # exit path left the provider subprocess running as an
                # orphan. Re-raises unchanged so cancellation semantics
                # aren't altered — this only adds the cleanup side effect.
                if proc.returncode is None:
                    await _terminate_process_group(proc, pgid)
                raise
    except WorkspaceLockBusy:
        raise RuntimeError(
            f"다른 역할 봇이 워크스페이스를 {WORKSPACE_LOCK_WAIT_SECONDS}초 넘게 사용 중이라 실행하지 못했습니다. 잠시 후 다시 말 걸어 주세요."
        )

    output = (stdout or b"").decode(errors="replace").strip()
    error = (stderr or b"").decode(errors="replace").strip()
    if proc.returncode != 0:
        # A tail-only slice can cut off the real cause for non-Python CLI
        # error formats (Python tracebacks put the exception message last,
        # but codex/agy's own error output doesn't always). Keep both ends.
        if len(error) > 1600:
            snippet = f"{error[:500]}\n…(생략)…\n{error[-1000:]}"
        else:
            snippet = error
        log(f"{role} exit={proc.returncode}: {snippet}")
        raise RuntimeError(f"{role} 실행에 실패했습니다. 로그를 확인해 주세요.")

    # Codex --json emits JSONL events.  Prefer the final assistant message,
    # while retaining a plain-text fallback for CLI version differences.
    if role == "codex" and output:
        import json

        candidates: list[str] = []
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    candidates.append(str(item["text"]))
            if event.get("type") == "response.completed":
                response = event.get("response") or {}
                if response.get("output_text"):
                    candidates.append(str(response["output_text"]))
        if candidates:
            output = candidates[-1].strip()

    if not output:
        detail = error or "CLI가 종료됐지만 stdout에 최종 응답을 쓰지 않았습니다."
        log(f"{role} empty response: {detail[-1600:]}")
        raise RuntimeError(f"{role}가 빈 응답을 반환했습니다: {detail[-500:]}")
    return output


async def run_provider(prompt: str, on_wait=None) -> str:
    return await _run_cli(ROLE, prompt, on_wait=on_wait)


async def generate_coding_plan(prompt: str, on_wait=None) -> str:
    plan_prompt = (
        "코드를 수정하지 말고 읽기 전용으로 조사하라. 아래 코딩 요청에 대해 "
        "실행 가능한 계획만 작성하라. 계획에는 목표, 범위, 변경 예정 파일, "
        "나노 작업, 검증 명령, 위험과 롤백 방법을 포함하라. 확인하지 않은 "
        "파일·명령·테스트 결과를 완료된 것처럼 말하지 말라.\n\n"
        f"[코딩 요청]\n{prompt}"
    )
    return await _run_cli(ROLE, plan_prompt, on_wait=on_wait, sandbox_mode="read-only")


# --- Codex-authors / Claude+Antigravity-verify loop ------------------------
#
# User-requested design (2026-07-31 Telegram group chat discussion): for
# messages to the codex bot that look like a coding task, don't just answer
# once. Instead: (1) codex implements, (2) claude and antigravity each
# independently and skeptically verify the actual workspace state (not just
# codex's own summary), (3) if either fails, codex revises based on the
# feedback and the loop repeats, up to CODEX_VERIFY_MAX_ROUNDS times.
#
# Deliberately NOT letting codex have the final word on whether a FAIL
# verdict is "valid" — codex judging criticism of its own work is a self-
# review bias risk (discussed and rejected in the same chat). Pass/fail is
# decided solely by parsing claude's and antigravity's own verdicts; codex
# only gets to act on that feedback, never overrule it. If both haven't
# agreed to PASS by the last round, the loop stops and hands the transcript
# to the human in the chat rather than declaring success on codex's say-so.
# Clamped to [1, 2]: the user explicitly decided on a 2-round cap (2026-07-31
# Telegram design discussion), so the env var can tune it down for testing
# but can't silently raise it past what was agreed, and a misconfigured 0
# can't collapse the loop to zero iterations (which would leave codex_report
# as the raw original prompt when the escalation message is built).
CODEX_VERIFY_MAX_ROUNDS = max(1, min(2, int(os.environ.get("TELEGRAM_AGENT_CODEX_VERIFY_MAX_ROUNDS", "2"))))

# Overall governor on top of the per-round-max above: even with the shorter
# verify-call timeout, a round can still legitimately run close to its worst
# case (codex authoring up to TIMEOUT_SECONDS + two verify calls up to
# CODEX_VERIFY_CALL_TIMEOUT_SECONDS each). Checked only *between* rounds —
# never aborts a round already in flight — so round 1 always gets to
# complete once for any request; this only decides whether round 2 is
# worth starting given how much of the budget round 1 already spent. Round-7
# independent review (Codex, 2026-07-31) asked for exactly this: a total-loop
# cap so _BUSY_LOCK can't be held for the full multi-round worst case.
CODEX_VERIFY_TOTAL_TIMEOUT_SECONDS = int(
    os.environ.get("TELEGRAM_AGENT_CODEX_VERIFY_TOTAL_TIMEOUT_SECONDS", "3600")
)

# Deliberately a coarse, low-cost keyword heuristic rather than an LLM call —
# this only decides "is the heavier verify loop worth it." False negatives
# (rare coding phrasing not in this list) just fall back to a normal single
# codex reply, which is cheap. False positives are NOT cheap, though: a
# false positive triggers up to CODEX_VERIFY_MAX_ROUNDS rounds of codex
# authoring plus a claude AND antigravity verify call each round — three
# providers and real wall-clock time for a message that never needed it.
# Earlier versions of this list included bare "수정"/"오류"/"에러"/"커밋" —
# words common in everyday non-coding chat ("이 오류 메시지가 뭐야?", "커밋
# 언제 할거야?") — which made false positives too easy to trigger. Round-7
# independent review (Codex + Antigravity, 2026-07-31) both flagged this and
# recommended requiring a coding-context word to co-occur rather than
# matching those four words alone; kept as compound phrases below instead
# of removing the concepts entirely.
_CODING_TASK_KEYWORDS = (
    "코드", "구현", "고쳐", "고쳐줘", "버그", "함수", "스크립트",
    "리팩터", "리팩토링", "짜줘", "만들어줘", "개발해", "디버그", "배포",
    "테스트 작성", "class ", "def ", "function",
    "코드 수정", "코드 오류", "코드 에러", "버그 수정", "커밋해줘", "커밋 해줘",
)


def _looks_like_coding_task(text: str) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in _CODING_TASK_KEYWORDS)


_VERDICT_MARKER = re.compile(r"RESULT:\s*(PASS|FAIL)", re.IGNORECASE)
_UNAVAILABLE_MARKER = re.compile(r"(session limit|rate limit|quota|not logged|인증|사용 한도|세션 한도)", re.IGNORECASE)


def _parse_verdict(verdict_text: str) -> bool | None:
    # Last marker wins (in case a model repeats/quotes the instruction).
    # None (no marker found at all) is treated as "not passed" by the
    # caller — an ambiguous verdict must never be read as a silent pass.
    match = None
    for match in _VERDICT_MARKER.finditer(verdict_text):
        pass
    if match is None:
        return None
    return match.group(1).upper() == "PASS"


def _provider_unavailable(text: str) -> bool:
    return bool(_UNAVAILABLE_MARKER.search(text or ""))


def _static_verify(workspace: Path) -> str:
    """Minimal non-provider fallback; never claims semantic correctness."""
    try:
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(workspace), "diff", "--check"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"정적 검증 실행 오류: {exc}"
    if result.returncode == 0:
        return "git diff --check 통과 (의미적 검증은 아님)"
    return f"git diff --check 실패: {(result.stdout or result.stderr).strip()[-500:]}"


def _build_verify_prompt(original_request: str, codex_report: str) -> str:
    return (
        "너는 이 Telegram 단체방에서 코덱스가 방금 수행한 코드 작업을 객관적이고 "
        "냉정하게 검증하는 역할이다. 코덱스의 자체 보고를 그대로 믿지 말고, 실제 "
        f"워크스페이스({_provider_workspace('codex')}) 파일을 직접 열어 확인하라 — git 저장소면 git diff/log로 "
        "실제 변경사항을 확인하고, 아니라면 관련 파일을 직접 읽어서 확인하라.\n\n"
        f"[원래 요청]\n{original_request}\n\n"
        f"[코덱스 자체 보고]\n{codex_report}\n\n"
        "요청이 실제로 올바르게 반영됐는지, 부작용이나 누락은 없는지 검증하고 발견한 "
        "문제점(있다면)을 구체적으로 적어라. 마지막 줄에 정확히 \"RESULT: PASS\" 또는 "
        "\"RESULT: FAIL\" 중 하나만 적어라."
    )


# Small first step toward cross-bot calling (2026-07-31): claude has no
# workspace-write sandbox of its own here (see _run_cli's "claude" branch —
# no -s/--sandbox flag, just a plain -p call), so a coding task addressed to
# claude is handed straight to codex, which already runs with
# workspace-write. Deliberately NOT a verify loop like codex_verify_and_revise
# — this is the smallest useful step (delegate, relay codex's own report)
# before building a general "any bot may call any bot" mechanism, which needs
# its own cycle/depth guards that don't exist yet.
async def claude_delegates_to_codex(original_prompt: str, message, on_wait=None) -> str:
    codex_report = await _run_cli("codex", original_prompt, on_wait=on_wait)
    return f"🔧 (코덱스에게 위임한 결과)\n\n{codex_report}"


async def codex_verify_and_revise(original_prompt: str, message, on_wait=None) -> str:
    codex_report = original_prompt
    last_claude_verdict = ""
    last_agy_verdict = ""
    loop_started = time.monotonic()

    for round_num in range(1, CODEX_VERIFY_MAX_ROUNDS + 1):
        if round_num > 1 and time.monotonic() - loop_started >= CODEX_VERIFY_TOTAL_TIMEOUT_SECONDS:
            log(
                f"codex_verify_and_revise 총 시간 상한({CODEX_VERIFY_TOTAL_TIMEOUT_SECONDS}초) 초과, "
                f"라운드{round_num} 생략하고 조기 종료"
            )
            break
        if round_num == 1:
            write_prompt = original_prompt
        else:
            write_prompt = (
                f"[원래 요청]\n{original_prompt}\n\n"
                f"[이전 라운드 클로드 검증]\n{last_claude_verdict}\n\n"
                f"[이전 라운드 안티그래비티 검증]\n{last_agy_verdict}\n\n"
                "위 검증 피드백을 반영해서 코드를 직접 수정하라. 무엇을 어떻게 "
                "고쳤는지 요약해서 보고하라."
            )
        codex_report = await run_provider(write_prompt, on_wait=on_wait)
        try:
            await message.reply_text(f"🔧 라운드{round_num}: 코덱스 작업 완료\n{codex_report[:1200]}")
        except TelegramError as exc:
            log(f"라운드 진행상황 전송 실패(계속 진행): {exc}")

        verify_prompt = _build_verify_prompt(original_prompt, codex_report)
        claude_available = True
        try:
            claude_verdict = await _run_cli(
                "claude", verify_prompt, on_wait=on_wait, timeout_seconds=CODEX_VERIFY_CALL_TIMEOUT_SECONDS
            )
        except Exception as exc:
            claude_available = False
            claude_verdict = f"RESULT: UNAVAILABLE\n(클로드 검증을 사용할 수 없음: {exc})"
        antigravity_available = True
        try:
            agy_verdict = await _run_cli(
                "antigravity", verify_prompt, on_wait=on_wait, timeout_seconds=CODEX_VERIFY_CALL_TIMEOUT_SECONDS
            )
        except Exception as exc:
            antigravity_available = False
            agy_verdict = f"RESULT: UNAVAILABLE\n(안티그래비티 검증을 사용할 수 없음: {exc})"
        last_claude_verdict, last_agy_verdict = claude_verdict, agy_verdict

        try:
            await message.reply_text(
                f"🔍 라운드{round_num} 검증\n[클로드]\n{claude_verdict[:900]}\n\n"
                f"[안티그래비티]\n{agy_verdict[:900]}"
            )
        except TelegramError as exc:
            log(f"검증 결과 전송 실패(계속 진행): {exc}")

        if _parse_verdict(claude_verdict) and _parse_verdict(agy_verdict):
            return f"✅ 라운드{round_num}에서 클로드·안티그래비티 검증 통과.\n\n{codex_report}"

        if (_parse_verdict(claude_verdict) and not antigravity_available) or (
            _parse_verdict(agy_verdict) and not claude_available
        ):
            static_result = _static_verify(_provider_workspace("codex"))
            return (
                f"⚠️ 부분 검증만 완료되었습니다 (라운드{round_num}).\n"
                "한 provider를 사용할 수 없어 독립 검증 합의는 성립하지 않았습니다.\n\n"
                f"[정적 검증] {static_result}\n"
                f"[클로드] {claude_verdict}\n\n[안티그래비티] {agy_verdict}\n\n"
                f"[최종 코덱스 작업]\n{codex_report}"
            )

    return (
        f"⚠️ {CODEX_VERIFY_MAX_ROUNDS}라운드까지 재수정했지만 클로드·안티그래비티 검증 "
        "합의에 이르지 못했습니다. 직접 판단해 주세요.\n\n"
        f"[최종 코덱스 작업]\n{codex_report}\n\n"
        f"[클로드 검증]\n{last_claude_verdict}\n\n[안티그래비티 검증]\n{last_agy_verdict}"
    )


# Guards a single role's own process against launching a second overlapping
# provider run (e.g. two quick messages, or a plain-chat message arriving
# mid-task) — fast, in-process, always-on. The separate cross-process
# concern (claude/codex/antigravity all running against the same shared
# WORKSPACE at once) is handled by acquire_workspace_lock() inside
# run_provider() instead: two independent reviews (Codex, Antigravity)
# both flagged the earlier "accept the race, it's rare" call as a real gap
# rather than a reasonable tradeoff, so it's now enforced — the second and
# third bot to grab the lock get an immediate "busy" reply instead of racing.
_BUSY_LOCK = asyncio.Lock()

_conflict_streak = 0
_conflict_times: "deque[float]" = deque(maxlen=200)
# A few isolated Conflicts are normal handoff noise (e.g. this exact process
# restarting — the old instance's long-poll takes a moment to fully release
# at Telegram's end; observed live, self-resolves). A DENSE burst instead
# means another process is actively polling with the same token right now.
# Distinguish by rate, not raw lifetime count, so occasional hiccups spread
# over hours don't trigger this.
CONFLICT_BURST_THRESHOLD = int(os.environ.get("TELEGRAM_AGENT_CONFLICT_BURST_THRESHOLD", "15"))
CONFLICT_BURST_WINDOW_SECONDS = int(os.environ.get("TELEGRAM_AGENT_CONFLICT_BURST_WINDOW_SECONDS", "120"))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _conflict_streak
    error = context.error
    if isinstance(error, Conflict):
        _conflict_streak += 1
        now = time.monotonic()
        _conflict_times.append(now)
        recent = sum(1 for t in _conflict_times if now - t <= CONFLICT_BURST_WINDOW_SECONDS)
        log(f"Conflict #{_conflict_streak} (최근 {CONFLICT_BURST_WINDOW_SECONDS}초 내 {recent}회) — 다른 프로세스가 같은 토큰으로 getUpdates 중일 수 있음: {error}")
        if recent >= CONFLICT_BURST_THRESHOLD:
            log(
                f"⚠️ Conflict가 {CONFLICT_BURST_WINDOW_SECONDS}초 내 {recent}회 발생 — 중복 poller로 판단, "
                "프로세스를 스스로 종료합니다 (launchd KeepAlive가 재기동)."
            )
            # If whatever is causing the conflict burst hasn't cleared (e.g.
            # a second poller is still up), the very next restart could hit
            # the same threshold within seconds and exit again — without
            # this, that's a tight self-restart loop burning CPU/network and
            # flooding the log (caught in round-6 independent review,
            # 2026-07-31). A short sleep here plus launchd's own
            # ThrottleInterval (set in the plist) are two independent
            # brakes on the same failure mode.
            await asyncio.sleep(10)
            context.application.stop_running()
        return
    _conflict_streak = 0
    if isinstance(error, NetworkError):
        log(f"NetworkError (PTB가 자동 재시도함): {error}")
        return
    tb = "".join(traceback.format_exception(type(error), error, error.__traceback__)) if error else ""
    log(f"미처리 예외: {error!r}\n{tb}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Username mismatch is checked once, hard, in _post_init (process exits
    # before ever reaching here if it's wrong) — no need to re-check per
    # message.
    text = addressed_text(update)
    if text is None:
        return

    message = update.effective_message
    if _is_stale(message):
        log(f"오래된 메시지 무시 chat={message.chat_id} age>{STALE_SECONDS}s")
        return

    if _BUSY_LOCK.locked():
        try:
            await message.reply_text(f"⏳ {ROLE_LABELS[ROLE]}가 이미 다른 작업을 처리 중이에요. 끝나면 답할게요.")
        except TelegramError as exc:
            log(f"busy 알림 전송 실패: {exc}")
        return

    async with _BUSY_LOCK:
        global ACTIVE_TASK_WORKSPACE, ACTIVE_LOGICAL_SESSION_ID
        user_id = message.from_user.id if message.from_user else "?"
        started = time.monotonic()
        log(f"처리 시작 chat={message.chat_id} user={user_id} text={text[:80]!r}")
        task_id = write_task_state(
            role=ROLE,
            chat_id=message.chat_id,
            text=text,
            status="started",
            workspace=str(CODEX_WORKSPACE if ROLE == "codex" else WORKSPACE),
            auth_source=_auth_source(ROLE),
        )
        try:
            ACTIVE_LOGICAL_SESSION_ID = start_session(
                task_id=task_id,
                channel="telegram",
                provider=ROLE,
                owner=f"telegram-{ROLE}",
                workspace=str(CODEX_WORKSPACE if ROLE == "codex" else WORKSPACE),
            )
        except Exception as exc:
            ACTIVE_LOGICAL_SESSION_ID = None
            write_task_state(role=ROLE, chat_id=message.chat_id, text=text, status="failed", task_id=task_id, error=str(exc)[-1000:])
            await message.reply_text(f"❌ 공통 세션 초기화 오류: {exc}")
            return

        if ROLE in ROLES:
            try:
                ACTIVE_TASK_WORKSPACE = _create_task_worktree(task_id)
                write_worktree_metadata(ACTIVE_TASK_WORKSPACE, task_id=task_id, role=ROLE)
                write_task_state(
                    role=ROLE,
                    chat_id=message.chat_id,
                    text=text,
                    status="started",
                    task_id=task_id,
                    workspace=str(ACTIVE_TASK_WORKSPACE),
                    auth_source=_auth_source(ROLE),
                )
                update_session(
                    ACTIVE_LOGICAL_SESSION_ID,
                    status="running",
                    workspace=str(ACTIVE_TASK_WORKSPACE),
                    worktree=str(ACTIVE_TASK_WORKSPACE),
                    event_type="worktree_bound",
                )
            except Exception as exc:
                ACTIVE_TASK_WORKSPACE = None
                if ACTIVE_LOGICAL_SESSION_ID:
                    update_session(ACTIVE_LOGICAL_SESSION_ID, status="failed", summary="텔레그램 작업공간 생성 실패", next_action="작업공간 생성 오류를 확인", event_type="worktree_failed")
                ACTIVE_LOGICAL_SESSION_ID = None
                write_task_state(role=ROLE, chat_id=message.chat_id, text=text, status="failed", task_id=task_id, error=str(exc)[-1000:])
                await message.reply_text(f"❌ Codex 작업공간 생성 오류: {exc}")
                return

        progress = None
        try:
            progress = await message.reply_text(f"⏳ {ROLE_LABELS[ROLE]} 처리 중...")
        except TelegramError as exc:
            log(f"progress 메시지 전송 실패(계속 진행): {exc}")

        async def _notify_waiting() -> None:
            if progress is None:
                return
            try:
                await progress.edit_text(
                    f"⏳ {ROLE_LABELS[ROLE]} 대기 중... (다른 역할 봇이 워크스페이스 사용 중이라 끝나면 이어서 처리해요)"
                )
            except TelegramError:
                pass

        # Split so a Telegram-side send/edit failure AFTER a successful
        # provider run can't get mislabeled as "provider execution error."
        # Before this split, one `except Exception` wrapped both the
        # provider call and the reply-sending loop, so e.g. a transient
        # `edit_text` BadRequest on an otherwise-successful run displayed
        # "❌ ... 실행 오류" — the task had actually succeeded. Caught in
        # round-6 independent review, 2026-07-31.
        try:
            if ROLE == "codex" and is_approval(text):
                pending = load_pending(message.chat_id)
                if pending is None:
                    raise RuntimeError("승인 대기 중인 계획이 없습니다. 먼저 코딩 요청을 보내세요.")
                clear_pending(message.chat_id)
                reply = await codex_verify_and_revise(str(pending["request"]), message, on_wait=_notify_waiting)
            elif ROLE == "codex" and _looks_like_coding_task(text):
                plan = await generate_coding_plan(text, on_wait=_notify_waiting)
                save_pending(
                    chat_id=message.chat_id,
                    task_id=task_id,
                    request=text,
                    plan=plan,
                    workspace=str(ACTIVE_TASK_WORKSPACE or CODEX_WORKSPACE),
                )
                reply = (
                    "📋 실행 계획을 만들었습니다. 아직 코드는 수정하지 않았습니다.\n\n"
                    f"{plan}\n\n"
                    "계획을 검토한 뒤 `실행 승인`이라고 보내면 구현을 시작합니다."
                )
            elif ROLE == "claude" and _looks_like_coding_task(text):
                reply = await claude_delegates_to_codex(text, message, on_wait=_notify_waiting)
            else:
                reply = await run_provider(text, on_wait=_notify_waiting)
        except Exception as exc:
            log(f"처리 실패 chat={message.chat_id} duration={time.monotonic() - started:.1f}s error={exc}")
            _record_telegram_efficiency(
                task_id=task_id,
                prompt=text,
                status="failed",
                started=started,
                workspace=ACTIVE_TASK_WORKSPACE,
            )
            write_task_state(role=ROLE, chat_id=message.chat_id, text=text, status="failed", task_id=task_id, error=str(exc)[-1000:])
            if ACTIVE_LOGICAL_SESSION_ID:
                update_session(
                    ACTIVE_LOGICAL_SESSION_ID,
                    status="failed",
                    summary=f"{ROLE} 실행 실패: {str(exc)[:1000]}",
                    next_action="실패 원인과 검증 결과를 확인",
                    event_type="provider_failed",
                )
            write_reflection(
                task_id=task_id,
                role=ROLE,
                workspace=str(ACTIVE_TASK_WORKSPACE or (CODEX_WORKSPACE if ROLE == "codex" else WORKSPACE)),
                status="failed",
                error=str(exc),
            )
            ACTIVE_TASK_WORKSPACE = None
            ACTIVE_LOGICAL_SESSION_ID = None
            error_text = f"❌ {ROLE_LABELS[ROLE]} 실행 오류: {exc}"
            try:
                if progress is not None:
                    await progress.edit_text(error_text)
                else:
                    await message.reply_text(error_text)
            except TelegramError as send_exc:
                log(f"오류 알림 전송도 실패: {send_exc}")
            return

        _record_telegram_efficiency(
            task_id=task_id,
            prompt=text,
            status="passed",
            output=reply,
            started=started,
            workspace=ACTIVE_TASK_WORKSPACE,
        )
        if ACTIVE_LOGICAL_SESSION_ID:
            update_session(
                ACTIVE_LOGICAL_SESSION_ID,
                status="succeeded",
                summary=reply[:8000],
                next_action="사용자 후속 요청 대기",
                workspace=str(CODEX_WORKSPACE if ROLE == "codex" else WORKSPACE),
                worktree=str(ACTIVE_TASK_WORKSPACE or ""),
                verification={"telegram_delivery_pending": True, "response_chars": len(reply)},
                event_type="provider_succeeded",
            )

        try:
            chunks = [reply[i:i + CHUNK_SIZE] for i in range(0, len(reply), CHUNK_SIZE)] or [""]
            truncated = len(chunks) > MAX_CHUNKS
            if truncated:
                chunks = chunks[:MAX_CHUNKS]
                chunks[-1] += f"\n\n… (응답이 너무 길어 {MAX_CHUNKS}개 메시지로 잘랐습니다)"
            for i, chunk in enumerate(chunks):
                if i == 0 and progress is not None:
                    await progress.edit_text(chunk)
                else:
                    await message.reply_text(chunk)
            write_task_state(
                role=ROLE,
                chat_id=message.chat_id,
                text=text,
                status="completed",
                task_id=task_id,
                response_preview=reply[:1000],
            )
            write_reflection(
                task_id=task_id,
                role=ROLE,
                workspace=str(ACTIVE_TASK_WORKSPACE or (CODEX_WORKSPACE if ROLE == "codex" else WORKSPACE)),
                status="completed",
                response_preview=reply,
            )
            ACTIVE_TASK_WORKSPACE = None
            ACTIVE_LOGICAL_SESSION_ID = None
            log(f"처리 완료 chat={message.chat_id} duration={time.monotonic() - started:.1f}s truncated={truncated}")
        except TelegramError as exc:
            # Provider already succeeded — do NOT show the "실행 오류"
            # error text here, that would misreport a delivery problem as a
            # task failure. Just log it; the user sees a stuck "⏳ 처리
            # 중..." message, which at least doesn't claim something false.
            ACTIVE_TASK_WORKSPACE = None
            ACTIVE_LOGICAL_SESSION_ID = None
            log(f"provider 성공했으나 텔레그램 전송 실패 chat={message.chat_id} duration={time.monotonic() - started:.1f}s error={exc}")


async def _post_init(application: Application) -> None:
    me = await application.bot.get_me()
    expected = ROLE_USERNAMES.get(ROLE, "").lower()
    actual = (me.username or "").lower()
    if expected and actual and expected != actual:
        # Warn-and-continue (the pre-round-5 behavior) is a real routing
        # hazard, not just cosmetic: addressed_text's mention/command
        # matching is keyed off ROLE_USERNAMES, so if the actual account
        # doesn't match, mentions/commands meant for THIS bot are never
        # recognized as addressed to it — the process stays alive and
        # silently mis-routes every explicitly-addressed message instead of
        # failing loudly. Fail fast so launchd's KeepAlive at least surfaces
        # the mismatch via repeated restarts in the log, rather than the
        # bot quietly running in a broken state indefinitely.
        log(f"치명적 설정 오류: ROLES[{ROLE!r}]['username']={expected!r} 이지만 실제 봇 계정은 @{actual} 입니다 — 종료합니다.")
        application.stop_running()
        os._exit(1)
    log(f"봇 사용자명 확인됨: @{actual}")


_SINGLETON_LOCK_DIR = Path.home() / ".claude" / "hooks-state" / "telegram-bridge-locks"
_singleton_lock_fd: int | None = None  # kept open (module-global, never closed) for the process's lifetime


def _acquire_singleton_lock() -> None:
    # Without this, a misconfigured second launchd job pointed at the same
    # token doesn't fail cleanly — both processes instead fight forever via
    # repeated Telegram Conflict errors, both eventually hit the burst
    # threshold, both restart, and neither ever stably wins (round-7
    # independent review, 2026-07-31). Failing fast here means the SECOND
    # process to start (the misconfiguration) exits immediately with a
    # clear reason in its own log, and the first keeps running undisturbed.
    global _singleton_lock_fd
    _SINGLETON_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
    lock_path = _SINGLETON_LOCK_DIR / f"singleton-{digest}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise SystemExit(
            "다른 프로세스가 이미 이 토큰으로 폴링 중입니다 (중복 launchd job 또는 token 파일 재사용 의심) — 종료합니다."
        )
    _singleton_lock_fd = fd


def main() -> None:
    _harden_log_permissions()
    log(f"Starting direct Telegram {ROLE} bot; workspace={(CODEX_WORKSPACE if ROLE == 'codex' else WORKSPACE)}; cli={CLI}")
    _acquire_singleton_lock()
    # Python 3.14 removed asyncio.get_event_loop()'s implicit loop creation,
    # which Application.run_polling() still relies on internally — without
    # this, every launchd instance crashes immediately with "There is no
    # current event loop in thread 'MainThread'."
    asyncio.set_event_loop(asyncio.new_event_loop())
    application = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )
    application.add_handler(MessageHandler(filters.ALL, handle_message))
    application.add_error_handler(on_error)
    application.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)
    # run_polling() only returns once something (e.g. on_error's conflict-burst
    # handler) calls application.stop_running(). PTB's own stop_running()
    # correctly stops its event loop (verified against library source,
    # 2026-07-31) and control returns here — but a lingering non-daemon
    # thread could in principle keep the interpreter alive past this point,
    # silently defeating the whole point of restarting on a conflict burst.
    # os._exit guarantees the process actually ends so launchd's KeepAlive
    # is guaranteed to relaunch it, regardless of that possibility.
    log("run_polling 종료 — 프로세스 재시작을 위해 종료합니다.")
    os._exit(1)


if __name__ == "__main__":
    main()
