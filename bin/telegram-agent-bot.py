#!/usr/bin/env python3
"""Direct Telegram group bridge for Claude, Codex, and Antigravity.

Each launchd instance has one Telegram token and one provider role.  The
bridge never calls OpenClaw: it invokes the provider CLI directly, using the
same workspace and subprocess environment as the Discord adapters.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, ContextTypes, MessageHandler, filters


HOME = Path.home()
WORKSPACE = Path(
    os.environ.get("TELEGRAM_AGENT_WORKSPACE", str(HOME / ".openclaw" / "workspace"))
).expanduser().resolve()
ROLE = os.environ.get("TELEGRAM_AGENT_ROLE", "").strip().lower()
TOKEN_FILE = Path(
    os.environ.get(
        "TELEGRAM_AGENT_TOKEN_FILE",
        str(HOME / ".config" / "agent-telegram" / f"{ROLE}.token"),
    )
).expanduser()
GROUP_TITLE = os.environ.get("TELEGRAM_AGENT_GROUP_TITLE", "edgeAI-agent")
TIMEOUT_SECONDS = int(os.environ.get("TELEGRAM_AGENT_TIMEOUT_SECONDS", "1800"))

BINARIES = {
    "claude": HOME / ".local" / "bin" / "claude",
    "codex": Path("/opt/homebrew/bin/codex"),
    "antigravity": HOME / ".local" / "bin" / "agy",
}

if ROLE not in BINARIES:
    raise SystemExit("TELEGRAM_AGENT_ROLE must be claude, codex, or antigravity")

CLI = BINARIES[ROLE]
if not TOKEN_FILE.exists():
    raise SystemExit(f"Telegram token file not found: {TOKEN_FILE}")
TOKEN = TOKEN_FILE.read_text(encoding="utf-8").strip()
if not TOKEN:
    raise SystemExit(f"Telegram token file is empty: {TOKEN_FILE}")

ENV = {
    **os.environ,
    "PATH": f"/opt/homebrew/bin:{HOME / '.local' / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
}

ROLE_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
    "antigravity": "Antigravity",
}


def addressed_text(update: Update, bot_username: str) -> str | None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return None
    if GROUP_TITLE and chat.title != GROUP_TITLE:
        return None
    text = message.text or message.caption or ""
    if not text:
        return None

    mention = f"@{bot_username.lower()}"
    command = re.compile(rf"^/(?:{re.escape(ROLE)})(?:@\S+)?(?:\s|$)", re.IGNORECASE)
    if mention not in text.lower() and not command.search(text):
        return None
    text = re.sub(re.escape(mention), "", text, flags=re.IGNORECASE)
    text = command.sub("", text, count=1).strip()
    return text or "간단히 자기소개하고, 내가 어떤 일을 맡기면 되는지 알려줘."


async def run_provider(prompt: str) -> str:
    if not CLI.exists():
        raise RuntimeError(f"provider executable is missing: {CLI}")

    if ROLE == "claude":
        args = [
            str(CLI), "-p", prompt, "--output-format", "text",
            "--append-system-prompt",
            "너는 이 Telegram 단체방의 Claude 담당 에이전트다. OpenClaw를 사용하지 말고 직접 작업하라.",
        ]
    elif ROLE == "codex":
        args = [
            str(CLI), "exec", "--json", "-s", "workspace-write",
            "-C", str(WORKSPACE), "--skip-git-repo-check", "--", prompt,
        ]
    else:
        args = [
            str(CLI), "--print", "--output-format", "text", prompt,
        ]

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(WORKSPACE),
        env=ENV,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"{ROLE} 실행 시간이 제한 시간({TIMEOUT_SECONDS}초)을 초과했습니다.")

    output = (stdout or b"").decode(errors="replace").strip()
    error = (stderr or b"").decode(errors="replace").strip()
    if proc.returncode != 0:
        print(f"{ROLE} exit={proc.returncode}: {error[-1200:]}", file=sys.stderr, flush=True)
        raise RuntimeError(f"{ROLE} 실행에 실패했습니다. 로그를 확인해 주세요.")

    # Codex --json emits JSONL events.  Prefer the final assistant message,
    # while retaining a plain-text fallback for CLI version differences.
    if ROLE == "codex" and output:
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
        raise RuntimeError(f"{ROLE}가 빈 응답을 반환했습니다.")
    return output


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_username = (context.bot.username or "").lower()
    text = addressed_text(update, bot_username)
    if text is None:
        return

    message = update.effective_message
    progress = await message.reply_text(f"⏳ {ROLE_LABELS[ROLE]} 처리 중...")
    try:
        reply = await run_provider(text)
        for start in range(0, len(reply), 3900):
            chunk = reply[start:start + 3900]
            if start == 0:
                await progress.edit_text(chunk)
            else:
                await message.reply_text(chunk)
    except Exception as exc:
        print(f"Telegram {ROLE} handler error: {exc}", file=sys.stderr, flush=True)
        await progress.edit_text(f"❌ {ROLE_LABELS[ROLE]} 실행 오류: {exc}")


def main() -> None:
    print(
        f"Starting direct Telegram {ROLE} bot; workspace={WORKSPACE}; cli={CLI}",
        file=sys.stderr,
        flush=True,
    )
    application = Application.builder().token(TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL, handle_message))
    application.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
