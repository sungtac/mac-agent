#!/usr/bin/env python3
"""Independent Telegram Roda bridge for the local Ollama Gemma model.

This bot deliberately has no shell, file, OpenClaw, or external-agent tools.
It only forwards approved Telegram text to Ollama and returns the response.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, ContextTypes, MessageHandler, filters


HOME = Path.home()
TOKEN_FILE = Path(os.environ.get("RODA_GEMMA_TOKEN_FILE", HOME / ".config/roda-gemma/telegram.token"))
OLLAMA_URL = os.environ.get("RODA_GEMMA_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("RODA_GEMMA_MODEL", "gemma4:latest")
RODA_USERNAME = os.environ.get("RODA_GEMMA_USERNAME", "sukja_hwpx_helper_bot").lstrip("@").lower()
ALLOWED_USER_IDS = {
    int(value) for value in os.environ.get("RODA_GEMMA_ALLOWED_USER_IDS", "6417205500").split(",") if value.strip()
}
ALLOWED_GROUP_IDS = {
    int(value) for value in os.environ.get("RODA_GEMMA_ALLOWED_GROUP_IDS", "-1003709316152").split(",") if value.strip()
}
MAX_PROMPT_CHARS = int(os.environ.get("RODA_GEMMA_MAX_PROMPT_CHARS", "6000"))
MAX_OUTPUT_TOKENS = int(os.environ.get("RODA_GEMMA_MAX_OUTPUT_TOKENS", "512"))
OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("RODA_GEMMA_OLLAMA_TIMEOUT_SECONDS", "120"))

SYSTEM_PROMPT = (
    "너는 로다(Roda)라는 독립적인 로컬 Gemma4 대화 봇이다. "
    "현재 역할은 짧고 정확한 대화와 간단한 안내뿐이다. "
    "파일 수정, 셸 명령, 시스템 조작, 외부 전송, 계정·인증 처리, 장기 계획 수립은 하지 않는다. "
    "그런 요청을 받으면 실행하지 말고 상위 에이전트의 계획과 별도 하네스가 필요하다고 짧게 말한다. "
    "확인하지 않은 실행 결과를 완료했다고 말하지 않는다."
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s roda-gemma: %(message)s")
log = logging.getLogger("roda-gemma")
for noisy_logger in ("httpx", "httpcore", "telegram", "telegram.ext"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)
REQUEST_SEMAPHORE = asyncio.Semaphore(1)


def _ollama_chat(prompt: str) -> str:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": MAX_OUTPUT_TOKENS},
    }
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("Ollama에 연결하지 못했습니다.") from exc
    if result.get("error"):
        raise RuntimeError("Gemma4 응답 오류가 발생했습니다.")
    answer = ((result.get("message") or {}).get("content") or "").strip()
    if not answer:
        raise RuntimeError("Gemma4가 빈 응답을 반환했습니다.")
    return answer


def _split_message(text: str, limit: int = 3800) -> list[str]:
    return [text[index : index + limit] for index in range(0, len(text), limit)] or [""]


def _strip_group_mention(text: str) -> str | None:
    mention = re.compile(rf"@{re.escape(RODA_USERNAME)}\b", re.IGNORECASE)
    if not mention.search(text):
        return None
    cleaned = mention.sub("", text, count=1).strip()
    return cleaned or "안녕하세요. 무엇을 도와드릴까요?"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    if user.id not in ALLOWED_USER_IDS:
        return

    text = (message.text or message.caption or "").strip()
    if not text:
        return
    if chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        if chat.id not in ALLOWED_GROUP_IDS:
            return
        text = _strip_group_mention(text)
        if text is None:
            return
    elif chat.type != ChatType.PRIVATE:
        return
    if len(text) > MAX_PROMPT_CHARS:
        text = text[:MAX_PROMPT_CHARS]

    await context.bot.send_chat_action(chat_id=chat.id, action="typing")
    async with REQUEST_SEMAPHORE:
        try:
            answer = await asyncio.to_thread(_ollama_chat, text)
        except Exception as exc:  # keep Telegram polling alive on provider errors
            log.warning("request failed: %s", exc)
            await message.reply_text("지금은 Gemma4에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.")
            return
    for part in _split_message(answer):
        await message.reply_text(part)


async def post_init(application: Application) -> None:
    me = await application.bot.get_me()
    actual = (me.username or "").lower()
    if actual != RODA_USERNAME:
        raise RuntimeError(f"Telegram 봇 계정이 예상과 다릅니다: @{actual}")
    log.info("connected as @%s; model=%s; ollama=%s", actual, MODEL, OLLAMA_URL)


def main() -> None:
    if not TOKEN_FILE.is_file():
        raise SystemExit(f"토큰 파일이 없습니다: {TOKEN_FILE}")
    if TOKEN_FILE.stat().st_mode & 0o077:
        raise SystemExit("토큰 파일 권한이 안전하지 않습니다.")
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("토큰 파일이 비어 있습니다.")
    # Python 3.14 no longer creates the main event loop implicitly, while
    # python-telegram-bot's run_polling() still expects one to exist.
    asyncio.set_event_loop(asyncio.new_event_loop())
    application = Application.builder().token(token).post_init(post_init).build()
    application.add_handler(MessageHandler(filters.TEXT | filters.CaptionRegex("."), handle_message))
    application.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
