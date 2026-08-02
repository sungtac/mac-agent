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
from telegram.ext import Application, ContextTypes, MessageHandler, TypeHandler, filters

from agent_profile import render_agent_profile
from edge_agent_context_envelope import ContextEnvelopeStore
from edge_agent_state import write_task_state
from edge_agent_skill_connector import build_skill_context


HOME = Path.home()
TOKEN_FILE = Path(os.environ.get("RODA_GEMMA_TOKEN_FILE", HOME / ".edge-agent/secrets/roda-gemma/telegram.token"))
OLLAMA_URL = os.environ.get("RODA_GEMMA_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("RODA_GEMMA_MODEL", "gemma4:latest")
RODA_USERNAME = os.environ.get("RODA_GEMMA_USERNAME", "sukja_hwpx_helper_bot").lstrip("@").lower()
RODA_WAKE_PATTERN = re.compile(
    r"(?<!\w)로다(?:에게|한테|야|아|는|가|랑|과|와|도|만|님)?(?!\w)",
    re.IGNORECASE,
)
GROUP_ADDRESS_WORDS = ("각자", "둘 다", "둘다", "다같이", "같이", "모두", "전부", "얘들아")
ALLOWED_USER_IDS = {
    int(value) for value in os.environ.get("RODA_GEMMA_ALLOWED_USER_IDS", "6417205500").split(",") if value.strip()
}
ALLOWED_GROUP_IDS = {
    int(value) for value in os.environ.get("RODA_GEMMA_ALLOWED_GROUP_IDS", "-1003952617795").split(",") if value.strip()
}
MAX_PROMPT_CHARS = int(os.environ.get("RODA_GEMMA_MAX_PROMPT_CHARS", "6000"))
MAX_OUTPUT_TOKENS = int(os.environ.get("RODA_GEMMA_MAX_OUTPUT_TOKENS", "512"))
OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("RODA_GEMMA_OLLAMA_TIMEOUT_SECONDS", "120"))
OLLAMA_THINK = os.environ.get("RODA_GEMMA_THINK", "0").strip().lower() in {"1", "true", "yes", "on"}

RODA_IDENTITY = render_agent_profile("roda")
SYSTEM_PROMPT = (
    f"{RODA_IDENTITY}\n\n"
    "너는 로다(Roda)라는 독립적인 로컬 Gemma4 대화 봇이다. "
    "현재 역할은 짧고 정확한 대화와 간단한 안내뿐이다. "
    "주입된 스킬 문서는 안전 규칙과 판단 기준일 뿐, 이 봇에 웹 검색이나 외부 도구 권한을 추가하지 않는다. "
    "실제 조회하지 않은 출처·검색 결과·실행 결과를 만들거나 완료했다고 말하지 않는다. "
    "파일 수정, 셸 명령, 시스템 조작, 외부 전송, 계정·인증 처리, 장기 계획 수립은 하지 않는다. "
    "그런 요청을 받으면 실행하지 말고 상위 에이전트의 계획과 별도 하네스가 필요하다고 짧게 말한다. "
    "확인하지 않은 실행 결과를 완료했다고 말하지 않는다."
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s roda-gemma: %(message)s")
log = logging.getLogger("roda-gemma")
for noisy_logger in ("httpx", "httpcore", "telegram", "telegram.ext"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)
REQUEST_SEMAPHORE = asyncio.Semaphore(1)


def _context_store() -> ContextEnvelopeStore:
    return ContextEnvelopeStore(os.environ.get("TELEGRAM_CONTEXT_ROOT") or os.environ.get("RODA_CONTEXT_ROOT") or None)


def _ollama_chat(prompt: str) -> str:
    skill_context = build_skill_context(prompt, max_chars=6000)
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{skill_context}".strip()},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        # Gemma4 can spend the whole output budget in its hidden thinking
        # field and return an empty final content.  Roda is a concise local
        # assistant by default, so keep thinking opt-in and preserve a usable
        # answer for Telegram.
        "think": OLLAMA_THINK,
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
    command = re.match(r"^/(\w+)(?:@([^\s]+))?(?:\s|$)", text)
    if command:
        target = command.group(2)
        # Slash commands addressed to another bot are not ordinary natural
        # language. In a shared group Roda must stay silent for them; without
        # this guard, disabling Telegram privacy mode makes Roda answer every
        # other bot's command as if it were default fan-out text.
        if not target or target.casefold() != RODA_USERNAME.casefold():
            return None
    mention = re.compile(rf"@{re.escape(RODA_USERNAME)}\b", re.IGNORECASE)
    addressed = mention.search(text) or RODA_WAKE_PATTERN.search(text)
    broadcast = any(word in " ".join(text.split()) for word in GROUP_ADDRESS_WORDS)
    if not addressed and not broadcast:
        # Shared-channel mode: once Telegram privacy mode is disabled (or the
        # bot is promoted), Roda participates in ordinary natural language too.
        return text.strip()
    cleaned = mention.sub("", text, count=1)
    cleaned = RODA_WAKE_PATTERN.sub("", cleaned, count=1).strip()
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
    original_text = text
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

    try:
        preparation = _context_store().prepare(
            channel="telegram",
            provider="gemma",
            chat_id=chat.id,
            message_id=getattr(message, "message_id", "0"),
            reply_to_message_id=getattr(getattr(message, "reply_to_message", None), "message_id", None),
            # The original user text is the only candidate source.  The helper
            # intentionally does not create a new anchor for short follow-ups.
            text=original_text,
        )
    except (OSError, ValueError, TypeError) as exc:
        log.warning("context preparation failed; continuing without anchor: %s", type(exc).__name__)
        preparation = None
    if preparation is not None and preparation.guard_required:
        guard = "⚠️ 이전 대상을 하나로 특정할 수 없습니다. 원본 메시지에 답장하거나 링크를 다시 보내 주세요." if preparation.resolution.status == "ambiguous" else "⚠️ 이전 대상이 만료되었습니다. 원본 메시지에 답장하거나 링크를 다시 보내 주세요."
        await message.reply_text(guard)
        return
    prompt = f"{preparation.prompt_block}\n\n[사용자 요청]\n{text}" if preparation is not None else text

    await context.bot.send_chat_action(chat_id=chat.id, action="typing")
    task_id = ""
    try:
        task_id = write_task_state(
            role="gemma", chat_id=chat.id, text=original_text, status="started",
            workspace="local-ollama", auth_source="telegram-token-file",
        )
    except (OSError, ValueError, TypeError) as exc:
        log.warning("coordination state start failed: %s", type(exc).__name__)
    async with REQUEST_SEMAPHORE:
        log.info("request started chat=%s", chat.id)
        try:
            answer = await asyncio.to_thread(_ollama_chat, prompt)
        except Exception as exc:  # keep Telegram polling alive on provider errors
            log.warning("request failed chat=%s: %s", chat.id, exc)
            try:
                write_task_state(role="gemma", chat_id=chat.id, text=original_text, status="failed", task_id=task_id, error=str(exc)[-500:])
            except (OSError, ValueError, TypeError):
                pass
            await message.reply_text("지금은 Gemma4에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.")
            return
    for part in _split_message(answer):
        sent = await message.reply_text(part)
        if preparation is not None and preparation.resolution.anchor is not None and getattr(sent, "message_id", None) is not None:
            _context_store().bind_response_message(
                channel="telegram",
                chat_id=chat.id,
                source_message_id=preparation.resolution.anchor.source_message_id,
                response_message_id=sent.message_id,
            )
    try:
        write_task_state(
            role="gemma", chat_id=chat.id, text=original_text, status="completed", task_id=task_id,
            response_tail=answer[-1000:], workspace="local-ollama", auth_source="telegram-token-file",
        )
    except (OSError, ValueError, TypeError) as exc:
        log.warning("coordination state completion failed: %s", type(exc).__name__)
    log.info("request completed chat=%s", chat.id)


async def log_update_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record update delivery metadata without logging message contents."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None:
        return
    text = (message.text or message.caption or "").strip()
    entities = tuple(message.entities or message.caption_entities or ())
    log.info(
        "update received update_id=%s chat=%s chat_type=%s user=%s has_text=%s text_length=%s entities=%s",
        getattr(update, "update_id", "unknown"),
        chat.id,
        chat.type,
        getattr(user, "id", "unknown"),
        bool(text),
        len(text),
        ",".join(str(getattr(entity, "type", "unknown")) for entity in entities) or "none",
    )


async def post_init(application: Application) -> None:
    me = await application.bot.get_me()
    actual = (me.username or "").lower()
    if actual != RODA_USERNAME:
        raise RuntimeError(f"Telegram 봇 계정이 예상과 다릅니다: @{actual}")
    can_read_all_group_messages = getattr(me, "can_read_all_group_messages", None)
    log.info(
        "connected as @%s; model=%s; ollama=%s; can_read_all_group_messages=%s",
        actual,
        MODEL,
        OLLAMA_URL,
        can_read_all_group_messages if can_read_all_group_messages is not None else "unknown",
    )
    # This is a diagnostic only.  It never changes Telegram permissions, but
    # makes the distinction between a live process and a bot that can receive
    # the configured group updates visible in the service log.
    for group_id in sorted(ALLOWED_GROUP_IDS):
        try:
            member = await application.bot.get_chat_member(group_id, me.id)
            membership = getattr(member, "status", "unknown")
            all_group_messages = can_read_all_group_messages is True or membership == "administrator"
            direct_mentions_expected = membership not in {"left", "kicked"}
            log.info(
                "group=%s membership=%s; all_group_messages_can_be_received=%s; direct_mentions_expected=%s",
                group_id,
                membership,
                all_group_messages,
                direct_mentions_expected,
            )
            if not all_group_messages and direct_mentions_expected:
                log.warning(
                    "group=%s privacy mode may filter unmentioned text; use @%s or promote the bot for full group intake",
                    group_id,
                    actual,
                )
        except Exception as exc:  # keep polling alive; health is still logged
            log.warning("group=%s membership check unavailable: %s", group_id, type(exc).__name__)


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
    application.add_handler(TypeHandler(Update, log_update_receipt), group=-1)
    application.add_handler(MessageHandler(filters.TEXT | filters.CaptionRegex("."), handle_message))
    application.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
