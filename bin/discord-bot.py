#!/usr/bin/env python3
"""Discord bot — Phase 1 (on-demand automation triggers) + Phase 2 v1
(reply-triggered retry for weekly-report.sh) + Phase 2.5 (reply-triggered
retry for work-log-stop-check.sh AND verify-task-v2.js's needs_clarification/
needsUserDecision escalations — see handle_pending_reply,
handle_verify_task_v2_retry, handle_verify_task_v2_decision_retry) + Phase 3
(free-form chat relay to `claude -p`, own session continuity via `--resume`
— see handle_free_chat and FREE_CHAT_* below).
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
Phase 3 free chat additionally gates on FREE_CHAT_USER_ID (channel trust
alone isn't enough for unrestricted full-tool-access `claude -p` relay).

Fixed 2026-07-29: this docstring previously claimed both "no free-form chat
relay (Phase 3 unimplemented)" and "verify-task-v2 clarification retry
remains unimplemented" — both were stale by the time they were read; the
code they described (FREE_CHAT_*, handle_verify_task_v2_retry) was already
built and working. See handle_verify_task_v2_retry's own docstring for how
that retry actually works without a resume/checkpoint mechanism (re-invokes
the whole workflow from scratch with the reply appended to the task text).

Codex-related commands (`!코덱스`, `!코덱스대화`, `!코덱스대화초기화`) lived
here through 2026-07-29, then moved to their own process, `bin/codex-bot.py`
— a separate bot identity/token, per the user's explicit request, so Codex
commands aren't answered by the same bot as Claude-side commands. This file
and codex-bot.py share `discord_bot_common.py` (SUBPROCESS_ENV,
usage_gate_check, _kill_process_group) rather than duplicating those.
"""
import asyncio
import datetime
import json
import os
import re
import sys
import uuid
from pathlib import Path

import discord

from discord_bot_common import SUBPROCESS_ENV, is_codex_wake_word, usage_gate_check, usage_headroom_advice, should_prefer_codex, run_provider_attempt, run_provider_fallback_chain, format_provider_fallback_failure, load_provider_context, save_provider_context, clear_provider_context, _kill_process_group, _kill_process_group_graceful, try_acquire_repo_lock, RepoLockBusy, fetch_cross_bot_context, MAC_BOT_PERSONA, QUOTA_LIMIT_PATTERN, atomic_write_json

CONFIG_PATH = Path.home() / ".claude" / "discord-bot" / "config.json"
MAC_AGENT = Path.home() / "mac-agent"
# Telegram's existing OpenClaw service is the canonical local workspace.  The
# Discord adapter must enter the same workspace so both channels see the same
# files, Team OS state, approval artifacts, and OpenClaw configuration boundary.
OPENCLAW_WORKSPACE = Path(
    os.environ.get("OPENCLAW_WORKSPACE", str(Path.home() / ".openclaw" / "workspace"))
).expanduser().resolve()
OPENCLAW_HOME = Path(
    os.environ.get("OPENCLAW_HOME", str(OPENCLAW_WORKSPACE.parent))
).expanduser().resolve()
WEEKLY_REPORT_SH = MAC_AGENT / "cron" / "weekly-report.sh"
WORK_LOG_STOP_CHECK_SH = MAC_AGENT / "hooks" / "work-log-stop-check.sh"
VERIFY_TASK_V2_JS = MAC_AGENT / "workflows" / "verify-task-v2.js"
CLAUDE_BIN = Path.home() / ".local" / "bin" / "claude"
PROVIDER_SANDBOX = MAC_AGENT / "bin" / "edge-agent-provider-sandbox.sh"
ANTIGRAVITY_BIN = Path.home() / ".local" / "bin" / "agy"
VERIFY_TASK_V2_RETRY_TIMEOUT_SECONDS = 30 * 60  # full-track verify-task-v2 round-trips codex/antigravity several times
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
    today = datetime.date.today().isoformat()

    weekly_log = STATE_DIR / "weekly-report" / f"{today}.log"
    worklog_dir = STATE_DIR / "work-log"

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
            # start_new_session + _kill_process_group, not plain proc.kill()
            # (2026-07-29 fix, matches handle_free_chat's already-fixed
            # pattern below in this same file): weekly-report.sh's own
            # `claude -p` sub-call is a grandchild of this proc, so a bare
            # kill() only kills the bash wrapper and orphans that headless
            # call running full-tool-access work undetected in the
            # background — the exact failure this repo's own
            # _kill_process_group() docstring confirms via live repro.
            start_new_session=True,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=WEEKLY_REPORT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            # graceful (SIGTERM-first) kill, not a bare _kill_process_group
            # (2026-07-30 fix): this wraps weekly-report.sh's own claude -p
            # call, which writes a report file with real tool access — a
            # bare SIGKILL mid-write risks a partially-written file exactly
            # when the timeout path most needs a clean state. codex-bot.py
            # already adopted this graceful variant for its own timeout
            # kills; this file's four full-tool-access claude -p handlers
            # had not been ported to it until now.
            await _kill_process_group_graceful(proc)
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
        elif proc.returncode == 4:
            # weekly-report.sh's usage-preflight-gate.sh check (2026-07-28):
            # skipped because account usage was too low to safely run, not a
            # failure. The script itself already posted its own
            # discord-notify.sh message + pending-job for this case, so this
            # ack (from the !주간보고서/reply-retry caller) would be a
            # duplicate notification — stay quiet here, same reasoning as
            # exit 3 just needing a distinct code to not be miscast.
            pass
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
            # start_new_session + _kill_process_group, not plain proc.kill()
            # (2026-07-30 fix, matches handle_weekly_report's pattern above
            # — this handler was the one spot in the file missing it):
            # work-log-stop-check.sh spawns its own grandchild
            # (usage-preflight-gate.sh -> coach) synchronously during the
            # exact window this timeout is meant to catch. A bare kill()
            # only kills this top-level bash wrapper and orphans that
            # grandchild if it's the thing actually hanging.
            start_new_session=True,
        )
        proc.stdin.write(stdin_json)
        await proc.stdin.drain()
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), timeout=45)
        if proc.returncode == 0:
            await message.channel.send(f"재시도 시작했습니다 (세션 {session_id}) — 완료되면 따로 알려드릴게요.")
        elif proc.returncode == 4:
            # work-log-stop-check.sh's own usage-preflight-gate.sh skip
            # (2026-07-30 fix, mirrors handle_weekly_report's exit-4
            # handling above): the script already sent its own
            # discord-notify.sh message + re-queued a fresh pending-job for
            # this case, so an ack here would be both a duplicate
            # notification AND actively wrong (this retry did NOT start —
            # it was skipped again). Stay quiet, same as weekly-report's
            # exit 3/4 handling.
            pass
        else:
            await message.channel.send(f"❌ work-log 재시도 디스패치 자체가 실패했습니다 (exit={proc.returncode}). 로그: ~/.claude/hooks-state/work-log/{session_id}.log")
    except asyncio.TimeoutError:
        _kill_process_group(proc)
        await message.channel.send(f"⚠️ work-log 재시도 디스패치가 45초 안에 반환되지 않았습니다 — 정상이라면 거의 즉시 반환돼야 하는데 이상 상황입니다. 세션 {session_id} 직접 확인이 필요합니다.")
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

    skip_reason = await usage_gate_check("claude")
    if skip_reason:
        await send_and_requeue(
            message,
            f"⏳ 지금 재시도를 건너뜁니다 — 계정 사용량 부족.\n{skip_reason}\n사용량 회복 후 이 메시지에 답변을 다시 담아 답장해주세요(방금 보낸 답변은 저장되지 않았습니다).",
            "verify-task-v2-retry",
            params,
        )
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

    # 2026-07-30 fix (사용자 확정, Codex 코드리뷰로 발견): codex-bot.py의
    # CODEX_DISPATCH_LOCKS는 그 프로세스 안에서만 유효해서, discord-bot.py의
    # 이 재시도가 codex-bot.py의 !코덱스와 거의 동시에 같은 저장소를 건드리는
    # 경우를 못 막았다. 크로스프로세스 파일 락으로 한 번 더 확인.
    try:
        with try_acquire_repo_lock(str(Path(cwd).resolve())):
            proc = await asyncio.create_subprocess_exec(
                str(PROVIDER_SANDBOX), str(CLAUDE_BIN), "-p", prompt, "--output-format", "text",
                env=verify_task_v2_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # start_new_session + _kill_process_group, not plain proc.kill()
                # (2026-07-29 fix, matches handle_free_chat's already-fixed
                # pattern below in this same file): this headless claude -p call
                # runs verify-task-v2.js, which itself spawns codex/antigravity
                # subprocess children — a bare kill() only kills this wrapper
                # and orphans those children running full-tool-access work
                # undetected in the background.
                start_new_session=True,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=VERIFY_TASK_V2_RETRY_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                # graceful kill (2026-07-30 fix, same rationale as
                # handle_weekly_report above): this claude -p run itself spawns
                # codex/antigravity subprocess children doing real file writes.
                await _kill_process_group_graceful(proc)
                await message.channel.send(f"⚠️ verify-task-v2 재시도가 {VERIFY_TASK_V2_RETRY_TIMEOUT_SECONDS}초를 넘어서 강제 종료했습니다 — 직접 확인이 필요합니다.")
                return
            tail = "\n".join((stdout or b"").decode(errors="replace").splitlines()[-20:])
            if proc.returncode == 0:
                await message.channel.send(f"✅ verify-task-v2 재시도 완료.\n```\n{tail}\n```"[:1900])
            else:
                await message.channel.send(f"❌ verify-task-v2 재시도 실패 (exit={proc.returncode}).\n```\n{tail}\n```"[:1900])
    except RepoLockBusy:
        await message.channel.send("이 저장소에 대한 다른 프로세스의 실행이 이미 진행 중입니다 — 잠시 후 이 메시지에 다시 답장해주세요.")
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
# 2026-07-29 fix: "필요없"/"필요 없"(붙여쓰기·한 칸 띄어쓰기)만 문자열매칭하던
# 것을, 한국어 조사가 그 사이에 끼는 경우(예: "다시 검토할 필요는 없고 수동으로
# 처리할게요"의 "필요는 없")도 잡도록 정규식으로 바꿨다. "필요"와 "없" 사이에
# 최대 2글자(조사·공백)까지 허용 — "필요없"(0글자)·"필요 없"(공백)·"필요는
# 없"(조사)·"필요도 없" 등을 모두 잡되, 임의 길이 문장 전체를 건너뛸 만큼
# 느슨하지는 않다. "하지마"류·"안 해도"류도 같은 이유로 정규식화했다. 영어
# 부정어·"그만"/"말고"/"말자"처럼 조사 삽입 문제가 구조적으로 없는 것들은
# 기존 문자열매칭 그대로 둔다.
# 2026-07-30 fix (실측 감사로 발견): {0,2}는 "필요까지는 없"(4글자: "까지는
# ") 같은 좀 더 긴 조사구를 놓쳤다 — 이 경우 실제로는 "재시도 필요 없음"을
# 뜻하는데도 매치가 안 돼서 원치 않는 자동 재시도가 나갈 수 있었다. {0,6}으로
# 넓혀서 이런 조사구까지 잡는다. 이 확장이 반대 방향 오탐(무관한 문장에서
# "필요"...6글자 이내...."없"이 우연히 만나는 경우)을 늘릴 순 있지만, 이 파일의
# fail-closed 설계(마커 매치=자동조치 안 함, 놓친 재시도<원치 않는 재시도)
# 덕분에 그 오탐의 결과는 "재시도를 한 번 더 요청해야 함" 정도로 안전한
# 방향이다 — 여전히 무한정 넓히지는 않음(문장 전체를 건너뛸 정도는 아님).
RETRY_NEGATION_PATTERNS = (
    re.compile(r"필요.{0,6}없"),
    re.compile(r"하지.{0,1}(마|말)"),
    re.compile(r"안.{0,1}해도"),
)
RETRY_NEGATION_MARKERS = (
    "그만", "말고", "말자",
    "no need", "don't", "dont ", "not necessary", "never mind", "nevermind",
)


def _has_retry_intent(reply_text: str) -> bool:
    lowered = reply_text.lower()
    if not any(kw in lowered for kw in RETRY_INTENT_KEYWORDS):
        return False
    if any(neg in lowered for neg in RETRY_NEGATION_MARKERS):
        return False
    if any(pat.search(lowered) for pat in RETRY_NEGATION_PATTERNS):
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

    skip_reason = await usage_gate_check("claude")
    if skip_reason:
        await send_and_requeue(
            message,
            f"⏳ 지금 재시도를 건너뜁니다 — 계정 사용량 부족.\n{skip_reason}\n사용량 회복 후 이 메시지에 재시도 키워드(재시도/retry/다시)를 담아 다시 답장해주세요.",
            "verify-task-v2-decision-retry",
            params,
        )
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

    # 2026-07-30 fix (사용자 확정, Codex 코드리뷰로 발견): codex-bot.py의
    # CODEX_DISPATCH_LOCKS는 그 프로세스 안에서만 유효해서, discord-bot.py의
    # 이 재시도가 codex-bot.py의 !코덱스와 거의 동시에 같은 저장소를 건드리는
    # 경우를 못 막았다. 크로스프로세스 파일 락으로 한 번 더 확인.
    try:
        with try_acquire_repo_lock(str(Path(cwd).resolve())):
            proc = await asyncio.create_subprocess_exec(
                str(PROVIDER_SANDBOX), str(CLAUDE_BIN), "-p", prompt, "--output-format", "text",
                env=verify_task_v2_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # start_new_session + _kill_process_group, not plain proc.kill()
                # (2026-07-29 fix, matches handle_free_chat's already-fixed
                # pattern below in this same file): this headless claude -p call
                # runs verify-task-v2.js, which itself spawns codex/antigravity
                # subprocess children — a bare kill() only kills this wrapper
                # and orphans those children running full-tool-access work
                # undetected in the background.
                start_new_session=True,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=VERIFY_TASK_V2_RETRY_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                # graceful kill (2026-07-30 fix, same rationale as
                # handle_weekly_report above): this claude -p run itself spawns
                # codex/antigravity subprocess children doing real file writes.
                await _kill_process_group_graceful(proc)
                await message.channel.send(f"⚠️ verify-task-v2 재시도가 {VERIFY_TASK_V2_RETRY_TIMEOUT_SECONDS}초를 넘어서 강제 종료했습니다 — 직접 확인이 필요합니다.")
                return
            tail = "\n".join((stdout or b"").decode(errors="replace").splitlines()[-20:])
            if proc.returncode == 0:
                await message.channel.send(f"✅ verify-task-v2 재시도 완료.\n```\n{tail}\n```"[:1900])
            else:
                await message.channel.send(f"❌ verify-task-v2 재시도 실패 (exit={proc.returncode}).\n```\n{tail}\n```"[:1900])
    except RepoLockBusy:
        await message.channel.send("이 저장소에 대한 다른 프로세스의 실행이 이미 진행 중입니다 — 잠시 후 이 메시지에 다시 답장해주세요.")
    except Exception as e:
        await message.channel.send(f"❌ verify-task-v2 재시도 실행 중 예외: {e}")


async def send_and_requeue(message: discord.Message, text: str, job_type: str, params: dict) -> None:
    """Send `text` and register a FRESH pending-job (same type/params) keyed
    to the message we just sent, so a reply to it re-triggers the same
    retry path. Needed because handle_pending_reply() deletes the inbound
    pending-job BEFORE dispatch ("delete first so a second reply can't
    double-trigger") — by the time a handler like
    handle_verify_task_v2_retry()/handle_verify_task_v2_decision_retry()
    decides to bail out on the usage gate, the original pending-job is
    already gone, so telling the user to "just reply again" would silently
    do nothing without this. Caught in code review, 2026-07-28, before it
    was ever hit live.
    """
    sent = await message.channel.send(text)
    job = {"type": job_type, "created_at": datetime.datetime.now().isoformat(), "params": params}
    atomic_write_json(PENDING_DIR / f"{sent.id}.json", job)


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

    # 2026-07-30 fix (Codex 코드리뷰로 발견, 사용자 확정): 이전엔 인가된
    # 채널의 누구나 pending-job 답장으로 재시도를 트리거할 수 있었다 —
    # verify-task-v2-retry/decision-retry는 결과적으로 풀-툴-액세스
    # claude -p 실행까지 이어지므로, 사실상 Phase 3 자유채팅과 동급 권한을
    # 채널 신뢰만으로 열어주고 있었다. free_chat_user_id(본인)로 제한해서
    # 자유채팅의 FREE_CHAT_USER_ID 게이트와 동일한 권한 레벨로 통일.
    # job은 안 건드림(삭제도, 만료 처리도 안 함) — 진짜 소유자가 나중에
    # 여전히 답장할 수 있어야 하고, 무단 답장 한 번으로 소유자의 재시도
    # 기회를 날려선 안 된다.
    if not FREE_CHAT_USER_ID or str(message.author.id) != FREE_CHAT_USER_ID:
        await message.channel.send("이 요청에는 답장할 권한이 없습니다.")
        return True

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
    params = job.get("params", {})
    # 2026-07-30 fix (found in an independent Codex code review): the
    # original code unconditionally unlinked the file BEFORE dispatching,
    # regardless of whether job_type/params were actually usable. An
    # unrecognized type or a non-dict params (corrupted file, or a producer
    # bug — verify-task-v2.js's own pending-job write is LLM-instruction-
    # mediated, not code-verified, see notifyDiscordEscalation) would let a
    # handler's own `params.get(...)` throw AFTER the file was already gone,
    # losing the retry state permanently with no user-visible feedback. The
    # "delete before dispatching" idempotency guard (so a second reply can't
    # double-trigger the same job) is still correct — it just needs to only
    # apply once the job is confirmed valid, not before.
    KNOWN_JOB_TYPES = {
        "weekly-report-retry", "work-log-retry",
        "verify-task-v2-retry", "verify-task-v2-decision-retry",
    }
    if job_type not in KNOWN_JOB_TYPES or not isinstance(params, dict):
        print(
            f"pending job {ref.message_id}: invalid (type={job_type!r}, "
            f"params type={type(params).__name__}) — keeping file, not dispatching",
            file=sys.stderr,
        )
        await message.channel.send(
            f"⚠️ 이 요청의 저장된 정보가 손상됐거나 인식할 수 없는 형식입니다 (type={job_type!r}) "
            "— 자동 재시도를 진행하지 않았습니다. 직접 확인이 필요합니다."
        )
        return True

    job_path.unlink(missing_ok=True)  # delete first so a second reply can't double-trigger — safe now that the job is confirmed valid

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
        await handle_work_log_retry(message, params)
        return True

    if job_type == "verify-task-v2-retry":
        await handle_verify_task_v2_retry(message, params)
        return True

    if job_type == "verify-task-v2-decision-retry":
        await handle_verify_task_v2_decision_retry(message, params)
        return True


def _sweep_expired_pending_jobs() -> int:
    """Startup sweep for pending-job files past PENDING_MAX_AGE_HOURS that
    nobody ever replied to. handle_pending_reply()'s own 48h check (above)
    only fires reactively when a reply arrives referencing that specific
    message — an escalation the user simply ignores (as opposed to
    explicitly declining) never triggers it, so it sat on disk forever.
    Found in integration audit (2026-07-30): one real orphan already
    existed, referencing a scratchpad `cwd` from a different, long-ended
    session. Runs once at startup, not a periodic loop — PENDING_DIR only
    grows between restarts and this bot is launchd KeepAlive'd (restarts
    often enough in practice that a startup-only sweep is sufficient; a
    long-uninterrupted run could still accumulate up to 48h+ of jobs before
    the next natural restart, which is an acceptable tradeoff over adding a
    background timer loop for what's currently a low-volume directory).
    """
    if not PENDING_DIR.exists():
        return 0
    removed = 0
    now = datetime.datetime.now()
    for job_path in PENDING_DIR.glob("*.json"):
        try:
            job = json.loads(job_path.read_text())
            created = datetime.datetime.fromisoformat(job["created_at"])
        except Exception:
            job_path.unlink(missing_ok=True)
            removed += 1
            continue
        if (now - created).total_seconds() > PENDING_MAX_AGE_HOURS * 3600:
            job_path.unlink(missing_ok=True)
            removed += 1
    return removed


@client.event
async def on_ready():
    print(f"logged in as {client.user} (id={client.user.id})")
    removed = _sweep_expired_pending_jobs()
    if removed:
        print(f"swept {removed} expired pending-job file(s)", file=sys.stderr)


FREE_CHAT_SESSION_FILE = Path.home() / ".claude" / "discord-bot" / "free-chat-session.json"
FREE_CHAT_FALLBACK_CONTEXT_FILE = Path.home() / ".claude" / "discord-bot" / "free-chat-fallback-context.json"
FREE_CHAT_CWD = OPENCLAW_WORKSPACE
FREE_CHAT_TIMEOUT_SECONDS = 30 * 60  # same budget as !코덱스/verify-task-v2 — full tool access can run a real coding task
# 2026-07-30, 사용자 명시적 요청: 맥이 계정 한도로 응답 자체가 실패하면
# "기다리세요"로 끝내지 말고, 이 저장소 다른 곳(route-dispatch.sh Rule B —
# "단순 작업은 안티그래비티 먼저, 실패하면 코덱스로 폴백")에 이미 있는
# 멀티에이전트 폴백 원칙을 그대로 적용해서 discord-bot.py 자신(파이썬,
# 맥의 claude -p 밖)이 같은 메시지를 대체 provider 체인으로 자동 재시도한다 — 맥 자신은
# claude -p 프로세스라 API 호출이 계정 한도로 막히면 내부에서 다른
# provider로 못 갈아탄다(자기 자신이 곧 그 호출이므로), 그래서 이 폴백은
# 반드시 감싸는 파이썬 코드 레벨에서 해야 한다. codex-bot.py도 같은
# 절대경로 상수를 자기 파일에 따로 갖고 있음(이 저장소에서 이미 흔한
# 패턴 — 완전 중앙화는 안 함).
CODEX_BIN = Path("/opt/homebrew/bin/codex")
CODEX_FALLBACK_TIMEOUT_SECONDS = 30 * 60  # 다른 provider 호출들과 동일 예산
ANTIGRAVITY_FALLBACK_TIMEOUT_SECONDS = 30 * 60
# A new free-chat session is the only safe place to rebalance: routing a
# resumed Claude session elsewhere would silently discard its conversation
# continuity.  Twenty percentage points is intentionally a wide hysteresis
# band, so a noisy coach reading does not make providers flap turn by turn.
FREE_CHAT_HEADROOM_MIN_MARGIN_PCT = 20
# 2026-07-30, 사용자 확정("짧은 Claude 재시도 후 대체 provider 체인") — 실측 근거:
# 실제 한도초과 거부(15:04:35)와 같은 계정의 바로 다음 정상 요청(15:04:44)
# 사이 간격이 9초였다. 10초면 그 순단을 넘기기에 충분하면서도 사용자를
# 오래 기다리게 하지 않는 값 — 정확한 과학적 근거보다는 실측 사례 하나에
# 여유를 조금 더한 값이라는 점은 명시해둔다.
CLAUDE_QUOTA_RETRY_DELAY_SECONDS = 10

# Concurrency guard (2026-07-29, found in review before ever hit live):
# handle_free_chat() reads the session id at the START and only writes it
# back at the END, after up to 30 minutes of awaiting a subprocess.
# discord.py dispatches on_message per-message as separate concurrent tasks
# (that's why e.g. !상태 still answers instantly while a long !주간보고서 is
# running), so a natural follow-up message sent before the first reply
# lands would read the SAME stale (pre-save) session id, spawn a second
# claude -p with a fresh random uuid of its own, and whichever finishes
# last silently overwrites the session file — the other reply is lost and
# the conversation the user thinks they're continuing quietly diverges.
# One process-wide lock is enough (this channel has exactly one free-chat
# user and one session file — no per-user keying needed). Rejects instead
# of queuing: a queued 30-minute-budget message piling up behind another
# is worse than just asking the user to wait and re-send.
FREE_CHAT_LOCK = asyncio.Lock()
FREE_CHAT_CURRENT_PROC = None  # the asyncio subprocess currently running under the lock, if any — lets !중지 kill it (see handle_free_chat_stop)
FREE_CHAT_STOP_REQUESTED = False


def _publish_free_chat_process(proc) -> None:
    global FREE_CHAT_CURRENT_PROC
    FREE_CHAT_CURRENT_PROC = proc


def _clear_free_chat_process(proc) -> None:
    global FREE_CHAT_CURRENT_PROC
    if FREE_CHAT_CURRENT_PROC is proc:
        FREE_CHAT_CURRENT_PROC = None


def _load_free_chat_session_id() -> str | None:
    try:
        return json.loads(FREE_CHAT_SESSION_FILE.read_text()).get("session_id")
    except Exception:
        return None


def _save_free_chat_session_id(session_id: str) -> None:
    FREE_CHAT_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    FREE_CHAT_SESSION_FILE.write_text(json.dumps({
        "session_id": session_id,
        "last_used_at": datetime.datetime.now().isoformat(),
    }))


async def handle_free_chat_stop(message: discord.Message):
    """!중지 — kills FREE_CHAT_CURRENT_PROC if a free-chat run is in flight.
    Only kills the OS subprocess, not the enclosing handle_free_chat()
    coroutine/task — its own `await proc.communicate()` simply returns once
    the killed process's pipes close, and the handler's existing
    `proc.returncode != 0` branch reports it as a failure, which is exactly
    what actually happened. No separate cancellation path needed.

    Checks FREE_CHAT_LOCK, not just FREE_CHAT_CURRENT_PROC (2026-07-29,
    caught in review before ever hit live): the lock is acquired BEFORE the
    usage gate check and subprocess spawn, so there's a real (short but
    nonzero — confirmed via a deliberately widened test) window where a run
    has genuinely started (lock held) but FREE_CHAT_CURRENT_PROC is still
    None because no OS process exists yet to kill. Without this, !중지
    during that window falsely claimed "nothing is running" even though one
    was actively starting.
    """
    if str(message.author.id) != FREE_CHAT_USER_ID:
        return
    try:
        global FREE_CHAT_STOP_REQUESTED
        if not FREE_CHAT_LOCK.locked():
            await message.channel.send("지금 실행 중인 응답이 없습니다.")
            return
        FREE_CHAT_STOP_REQUESTED = True
        if FREE_CHAT_CURRENT_PROC is not None:
            # graceful kill (2026-07-30 fix, same rationale as the timeout
            # paths above): a user-initiated stop carries the exact same
            # mid-write SIGKILL corruption risk as a timeout-triggered one —
            # this is a full-tool-access claude -p run either way. Adds up
            # to ~2s before the process is confirmed dead; worth it.
            await _kill_process_group_graceful(FREE_CHAT_CURRENT_PROC)
            await message.channel.send("중단 요청을 보냈습니다.")
        else:
            await message.channel.send("중단 요청을 기록했습니다 — 현재 provider가 끝나는 즉시 다음 provider로 넘어가지 않습니다.")
    except Exception as e:
        # 2026-07-29 fix: `_kill_process_group`의 os.killpg 실패 폴백인
        # `proc.kill()`도 프로세스가 이미 완전히 종료된 상태면 자체적으로
        # ProcessLookupError를 던질 수 있다(예: !중지를 연달아 두 번 보내는
        # 경우) — on_message는 이걸 감싸는 예외처리가 없어서 그대로 전파되면
        # 사용자는 아무 응답도 못 받는다(이 저장소가 이미 codex-bot.py의
        # handle_codex_chat_reset에서 동일 클래스 문제를 고치며 확립한 "무응답
        # 절대 금지" 관례를 여기도 적용).
        await message.channel.send(f"❌ 중단 처리 중 예외: {e}")


async def handle_free_chat_reset(message: discord.Message):
    # Gated the same way handle_codex_dispatch gates itself (checked inside
    # the handler, not left to the on_message dispatch site) — caught in
    # review: unlike handle_free_chat, on_message's "!새대화" branch has no
    # author-id condition of its own, so without this check any channel
    # member could reset the one authorized user's conversation state.
    if str(message.author.id) != FREE_CHAT_USER_ID:
        return
    # Also caught in review (2026-07-29): resetting while a run is still in
    # flight used to be silently undone — that run's own eventual
    # _save_free_chat_session_id() call would land AFTER this delete and
    # re-establish the old (or a stale new) session, making the reset look
    # like it worked but not actually stick. Reusing FREE_CHAT_LOCK (the
    # same lock handle_free_chat holds for its whole duration) means this
    # can't run concurrently with that save.
    if FREE_CHAT_LOCK.locked():
        await message.channel.send("지금 응답을 처리 중이라 초기화할 수 없습니다 — 끝나거나 `!중지`한 뒤 다시 시도해주세요.")
        return
    FREE_CHAT_SESSION_FILE.unlink(missing_ok=True)
    clear_provider_context(FREE_CHAT_FALLBACK_CONTEXT_FILE)
    await message.channel.send("대화를 초기화했습니다 — 다음 메시지부터 새 대화로 시작합니다.")


async def handle_free_chat(message: discord.Message):
    """Phase 3 (2026-07-28/29): relay any non-command message from
    FREE_CHAT_USER_ID straight to a headless `claude -p` with full tool
    access (Edit/Write/Bash, same as an interactive session — no repo
    allowlist like !코덱스 has, since the user explicitly chose the
    broader-trust option here; FREE_CHAT_USER_ID is the only boundary).
    No prefix required — every message from that user in this channel that
    isn't a recognized command or a pending-job reply gets relayed
    (user's explicit choice over requiring e.g. "!채팅 ..." — closer to the
    Cowork-style natural chat this was modeled on).

    Session continuity via `--resume`/`--session-id`: a session id is
    generated once (Python's own uuid, not parsed from Claude's output) and
    persisted to FREE_CHAT_SESSION_FILE. Every later message resumes that
    same session, so context carries across separate Discord messages the
    way an ongoing conversation would. `!새대화` (handle_free_chat_reset)
    deletes the state file to start a fresh session on demand — necessary
    once conversations persist, otherwise there's no way to change topics
    without dragging the whole history along.

    No --permission-mode override — deliberately matches every other
    headless `claude -p` call in this file (weekly-report.sh,
    kakao-morning-briefing.sh, work-log-stop-check.sh, both verify-task-v2
    retry handlers): none of them set one either, relying on whatever
    non-interactive default + this Mac's own ~/.claude/settings.json
    permission config already resolves tool approval to. Introducing a new
    permission mode just for this handler would be untested territory this
    review didn't have session-limit budget to verify live.

    Concurrency: rejects instead of queuing if FREE_CHAT_LOCK is already
    held (see that constant's comment for the race this closes) — checked
    BEFORE the usage gate so a busy reply doesn't also cost a gate-script
    spawn. Holds the lock for the entire subprocess lifetime, including the
    usage gate check, so two messages can never race on session-id
    load/save. FREE_CHAT_CURRENT_PROC is published while held so
    handle_free_chat_stop() (!중지) has something to kill.
    """
    if FREE_CHAT_LOCK.locked():
        await message.channel.send("이전 메시지를 아직 처리 중입니다 — 끝나면 다시 말씀해주세요 (중단하려면 `!중지`).")
        return

    global FREE_CHAT_STOP_REQUESTED
    async with FREE_CHAT_LOCK:
        FREE_CHAT_STOP_REQUESTED = False
        text = message.content.strip()
        if not text:
            return

        skip_reason = await usage_gate_check("claude")
        if skip_reason:
            # 2026-07-30, 사용자 명시적 요청: 사전 게이트에서 이미 낮다고
            # 판단된 경우에도 실행 후 실패 케이스와 동일하게 코덱스로
            # 자동 폴백 — "계정 한도 낮음"을 감지하는 두 지점(사전 게이트 vs
            # 사후 실패 문구 매칭) 모두 같은 멀티에이전트 폴백 원칙을 따르게
            # 통일한다.
            await message.channel.send(f"⏳ 맥(Claude) 계정 사용량 부족으로 대체 provider 체인으로 전환합니다.\n{skip_reason}")
            await _fallback_to_provider_chain(message, text)
            return
        if FREE_CHAT_STOP_REQUESTED:
            return

        existing_session_id = _load_free_chat_session_id()
        is_new_session = existing_session_id is None
        session_id = existing_session_id or str(uuid.uuid4())

        # 2026-07-30, active headroom balancing: usage-advisor.sh used to be
        # advisory only, so a new Claude conversation still consumed Claude
        # even when Codex had materially more remaining capacity.  Apply the
        # comparison only before a new session starts.  A resumed Claude
        # session stays on Claude because moving it would discard context and
        # make the user's conversation appear to reset.  Unknown/stale
        # advisor data fails open and keeps the proven Claude path.
        if is_new_session:
            advice = await usage_headroom_advice()
            if should_prefer_codex(advice, FREE_CHAT_HEADROOM_MIN_MARGIN_PCT):
                await message.channel.send(
                    "⚖️ 새 대화는 잔여량 균형을 위해 Claude 대신 대체 provider 체인으로 시작합니다."
                )
                await _fallback_to_provider_chain(message, text)
                return

        # 2026-07-30, 사용자 명시적 요청("이 부분을 기억해달라"): 자유채팅의
        # --resume 세션은 그대로 독립 유지하되(세션 병합 아님), 같은 채널에서
        # codex-bot.py와 나눈 대화를 이 턴의 프롬프트에 참고자료로 얹어준다 —
        # 그래야 사용자가 "방금 코덱스한테 뭐라고 했잖아" 식으로 말해도 맥락이
        # 통한다. 채널 히스토리만 읽는 것이지 Codex의 실제 스레드 상태를
        # 건드리지 않으므로 크로스프로세스 쓰기 경쟁과는 무관.
        cross_context = await fetch_cross_bot_context(message.channel, client.user.id)
        fallback_context = load_provider_context(FREE_CHAT_FALLBACK_CONTEXT_FILE)
        context_blocks = []
        if fallback_context:
            context_blocks.append(format_provider_context(fallback_context))
        if cross_context:
            context_blocks.append(
                "[참고 — 같은 Discord 채널에서 최근 다른 봇(코덱스)과 나눈 대화. "
                "네 실제 세션 기록이 아니라 곁눈질로 보는 참고자료일 뿐이니, "
                "여기 내용을 사실로 단정하지 말고 필요할 때만 자연스럽게 참고해:]\n"
                f"{cross_context}"
            )
        prompt_text = "\n\n".join(context_blocks + [f"[사용자 메시지]\n{text}"]) if context_blocks else text
        if FREE_CHAT_STOP_REQUESTED:
            return

        env = {
            **SUBPROCESS_ENV,
            "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0",
            "OPENCLAW_HOME": str(OPENCLAW_HOME),
            "OPENCLAW_WORKSPACE": str(OPENCLAW_WORKSPACE),
            "PYTHONPATH": os.pathsep.join(
                part for part in (str(OPENCLAW_WORKSPACE), SUBPROCESS_ENV.get("PYTHONPATH", "")) if part
            ),
        }
        # --append-system-prompt는 매 턴(resume 포함) 넣는다 — Claude
        # 쪽엔 이 페르소나를 스레드 상태처럼 한 번만 넣고 재사용할 방법이
        # 없고(-p는 매번 새 프로세스, resume은 대화 기록만 이어받지 CLI
        # 플래그를 기억하진 않음), 매번 같은 텍스트라 프롬프트 캐시
        # 관점에서도 손해가 없다(codex-bot.py의 CODEX_BOT_PERSONA는 반대로
        # 새 스레드 때만 넣음 — 코덱스는 이게 시스템프롬프트가 아니라
        # 대화 본문 자체라 매턴 반복하면 노이즈로 쌓이기 때문, 그 차이는
        # discord_bot_common.py의 두 상수 주석 참고).
        args = [str(PROVIDER_SANDBOX), str(CLAUDE_BIN), "-p", prompt_text, "--output-format", "text", "--append-system-prompt", MAC_BOT_PERSONA]
        args += ["--session-id", session_id] if is_new_session else ["--resume", session_id]

        global FREE_CHAT_CURRENT_PROC

        async def _run_once():
            """스폰+통신 1회. 성공/실패 상관없이 (returncode, out_text)를
            반환하고, 타임아웃이면 직접 메시지를 보낸 뒤 None을 반환한다
            (호출자는 그 자리에서 끝내야 함) — 재시도 로직이 이 스폰
            시퀀스를 두 번 호출해야 해서 함수로 뽑음.

            start_new_session=True (2026-07-29, found in review): makes
            this process its own group leader, so _kill_process_group()
            (used by both the timeout below and handle_free_chat_stop's
            !중지) can clean up anything IT spawns too — full tool access
            means a Bash call here can easily start a long-running child
            that a plain proc.kill() would just orphan (confirmed via a
            local repro before this fix: proc.kill() left a backgrounded
            grandchild alive; start_new_session + os.killpg left nothing).
            """
            global FREE_CHAT_CURRENT_PROC
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=str(FREE_CHAT_CWD), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            FREE_CHAT_CURRENT_PROC = proc
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=FREE_CHAT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                # graceful kill (2026-07-30 fix, same rationale as above).
                await _kill_process_group_graceful(proc)
                await message.channel.send(f"⚠️ 응답이 {FREE_CHAT_TIMEOUT_SECONDS // 60}분을 넘어서 강제 종료했습니다 — 직접 확인이 필요합니다.")
                return None
            finally:
                _clear_free_chat_process(proc)
            return proc.returncode, (stdout or b"").decode(errors="replace").strip()

        try:
            result = await _run_once()
            if result is None:
                return
            returncode, out_text = result
            if FREE_CHAT_STOP_REQUESTED:
                return

            if returncode != 0 and QUOTA_LIMIT_PATTERN.search(out_text):
                # 2026-07-30, 사용자 확정: 실측으로 확인된 실제 사례 —
                # 같은 계정이 15:04:35에 이 한도초과로 거부됐는데, 같은
                # 계정의 바로 다음 요청은 단 9초 뒤(15:04:44)에 캐시적중률
                # 100%로 정상 통과했다(트랜스크립트 usage 필드 직접 대조로
                # 확인). 즉 계정이 진짜로 소진된 게 아니라 순간적인 순단인
                # 경우가 실제로 있다 — 코덱스로 넘기기 전에 같은 Claude로
                # 한 번 더 짧게 재시도해서, 그런 순단은 여기서 조용히
                # 흡수한다. 그래도 안 되면(진짜 7일창 소진 등) 아래에서
                # Antigravity를 먼저 거친 뒤 Codex 폴백으로 넘어간다.
                await asyncio.sleep(CLAUDE_QUOTA_RETRY_DELAY_SECONDS)
                if FREE_CHAT_STOP_REQUESTED:
                    return
                retry_result = await _run_once()
                if retry_result is None:
                    return
                returncode, out_text = retry_result

            if returncode != 0:
                # 2026-07-30, 사용자 명시적 요청("맥은 실제 계정을 따라가지
                # 않고, 멀티에이전트를 따라가야 하는데"): 계정 한도 초과를
                # 그냥 "기다리세요"로 끝내는 건 시스템 전체가 죽은 것처럼
                # 취급하는 단일장애점식 사고다 — 이 저장소 다른 곳
                # (route-dispatch.sh Rule B: "단순 작업은 안티그래비티
                # 먼저, 실패하면 코덱스로 폴백")에 이미 있는 멀티에이전트
                # 폴백 원칙을 맥의 실패 처리에도 반영한다. 맥 자신(claude -p)은
                # API 호출이 막히면 내부에서 못 갈아타므로(자기 자신이 곧
                # 그 호출), 감싸는 이 파이썬 코드가 같은 메시지를 코덱스로
                # 자동 재시도한다.
                if QUOTA_LIMIT_PATTERN.search(out_text):
                    await message.channel.send(f"⏳ 맥(Claude)이 계정 사용 한도로 응답하지 못했습니다(짧은 재시도도 실패) — 대체 provider 체인으로 자동 전환합니다.\n```\n{out_text[-300:]}\n```")
                    await _fallback_to_provider_chain(message, text)
                else:
                    await message.channel.send(f"❌ 실패 (exit={returncode}).\n```\n{out_text[-1500:]}\n```"[:1900])
                return
            # Only persist the session id AFTER a successful run — an id from a
            # run that errored out (e.g. Claude Code couldn't start at all)
            # would just make every subsequent message resume a session that
            # never really began.
            _save_free_chat_session_id(session_id)
            clear_provider_context(FREE_CHAT_FALLBACK_CONTEXT_FILE)
            await message.channel.send(out_text[:1900] if out_text else "(응답 없음)")
        except Exception as e:
            await message.channel.send(f"❌ 실행 중 예외: {e}")
        finally:
            FREE_CHAT_CURRENT_PROC = None
            FREE_CHAT_STOP_REQUESTED = False


async def _fallback_to_provider_chain(message: discord.Message, text: str) -> None:
    """Run the fallback chain Antigravity -> Codex for one user message.

    Antigravity has no reliable usage number in `coach`, so it is tried
    optimistically and classified by its process result. Codex remains behind
    its own preflight gate.
    """
    if FREE_CHAT_STOP_REQUESTED:
        return
    cross_context = await fetch_cross_bot_context(message.channel, client.user.id)
    fallback_note = (
        "[알림: '맥'(Claude)이 지금 계정 사용 한도로 응답할 수 없어서, 네가 다음 provider로 "
        "대신 사용자 요청에 최선을 다해 답해줘. 이 폴백 경로는 읽기/응답 전용이므로 파일을 "
        "수정하지 마. 코드 변경 요청이면 필요한 변경안과 검증 방법만 설명하고, 실제 수정은 "
        "Claude 복귀 후 verify-task-v2 경로로 진행해야 해. 맥에게 다시 위임하지 마.]"
    )
    if cross_context:
        prompt_text = f"{fallback_note}\n\n[참고 — 같은 채널 최근 대화]\n{cross_context}\n\n[사용자 메시지]\n{text}"
    else:
        prompt_text = f"{fallback_note}\n\n[사용자 메시지]\n{text}"

    try:
        async def antigravity_attempt():
            return await run_provider_attempt(
                "antigravity",
                [
                    str(ANTIGRAVITY_BIN), "--print", "--mode", "plan",
                    "--print-timeout", f"{ANTIGRAVITY_FALLBACK_TIMEOUT_SECONDS // 60}m",
                    "-p", prompt_text,
                ],
                ANTIGRAVITY_FALLBACK_TIMEOUT_SECONDS,
                cwd=FREE_CHAT_CWD,
                on_process_started=_publish_free_chat_process,
                on_process_finished=_clear_free_chat_process,
            )

        async def codex_attempt():
            return await run_provider_attempt(
                "codex",
                [
                    str(PROVIDER_SANDBOX), str(CODEX_BIN), "exec", "-s", "read-only",
                    "-C", str(FREE_CHAT_CWD), "--skip-git-repo-check", "--", prompt_text,
                ],
                CODEX_FALLBACK_TIMEOUT_SECONDS,
                cwd=FREE_CHAT_CWD,
                on_process_started=_publish_free_chat_process,
                on_process_finished=_clear_free_chat_process,
            )

        chain = await run_provider_fallback_chain(
            antigravity_attempt,
            lambda: usage_gate_check("codex"),
            codex_attempt,
            should_continue=lambda: not FREE_CHAT_STOP_REQUESTED,
        )
        antigravity_result = chain.antigravity
        if chain.stop_reason or FREE_CHAT_STOP_REQUESTED:
            return
        if antigravity_result.usable:
            save_provider_context(FREE_CHAT_FALLBACK_CONTEXT_FILE, "antigravity", text, antigravity_result.output)
            await message.channel.send(
                f"🔀 (맥 대신 안티그래비티가 응답)\n{antigravity_result.output}"[:1900]
            )
            return

        if chain.codex_skip_reason:
            await message.channel.send(format_provider_fallback_failure(chain))
            return

        codex_result = chain.codex
        if codex_result is not None and codex_result.usable:
            save_provider_context(FREE_CHAT_FALLBACK_CONTEXT_FILE, "codex", text, codex_result.output)
            await message.channel.send(f"🔀 (맥/안티그래비티 대신 코덱스가 응답)\n{codex_result.output}"[:1900])
            return

        await message.channel.send(format_provider_fallback_failure(chain))
    except Exception as e:
        await message.channel.send(f"❌ 대체 provider 체인 실행 중 예외: {e}")


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
    elif content == "!새대화":
        await handle_free_chat_reset(message)
    elif content == "!중지":
        await handle_free_chat_stop(message)
    elif is_codex_wake_word(content) or content.startswith("!코덱스"):
        # 2026-07-29, widened 2026-07-30 (실측 감사로 발견): a message meant
        # for Codex must NOT also get answered here — codex-bot.py handles
        # it instead. Both bots sit in the same channel and see every
        # message, so without this exclusion it gets TWO replies (this bot's
        # free-chat catch-all below has no content filter of its own).
        # Originally this only excluded the bare wake-word form ("코덱스야
        # ...", "콕스 ..."). That missed every "!"-prefixed command form
        # ("!코덱스", "!코덱스대화", "!코덱스대화초기화") — those all start
        # with "!", not with the bare word, so `startswith(CODEX_CHAT_WAKE_WORDS)`
        # was False and every single use of any of the three codex-bot.py
        # commands fell straight through to the free-chat branch below,
        # firing a SECOND, uncoordinated, unrestricted-full-tool-access
        # `claude -p` relay on the same text — worse than a cosmetic double
        # reply, since free-chat has no `CODEX_REPO_ALIASES` write-scope
        # guard the way codex-bot.py's dispatch does. `"!코덱스"` covers all
        # three command forms since they share that prefix.
        # Widened again 2026-07-30 (실측 버그): startswith alone missed wake
        # words that aren't the first token (e.g. "안녕 콕스") — see
        # is_codex_wake_word()'s docstring in discord_bot_common.py.
        pass
    elif FREE_CHAT_USER_ID and str(message.author.id) == FREE_CHAT_USER_ID:
        # Phase 3: anything else from the one authorized user is free chat.
        # Everyone else's non-command messages are still silently ignored —
        # channel-wide trust covers commands, not arbitrary-instruction relay.
        await handle_free_chat(message)


if __name__ == "__main__":
    client.run(CONFIG["token"])
