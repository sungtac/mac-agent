#!/usr/bin/env python3
"""Independent Telegram Roda bridge for the local Ollama Gemma model.

This bot deliberately has no shell, file, OpenClaw, or external-agent tools.
It only forwards approved Telegram text to Ollama and returns the response.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from telegram import Update
from telegram.constants import ChatType
from telegram.error import RetryAfter
from telegram.ext import Application, ContextTypes, MessageHandler, TypeHandler, filters

from agent_profile import render_agent_profile
from edge_agent_context_envelope import ContextEnvelopeStore
from edge_agent_channel_runtime import build_shared_context
from edge_agent_control_plane import ControlPlaneError, ControlPlaneStore, is_cancel_request
from edge_agent_deliberation import (
    DeliberationStore,
    configured_barrier_timeout_seconds,
    roles_for_request,
    session_id_for_telegram,
    should_publish_user_result,
)
from edge_agent_delegation import (
    DelegationStore,
    is_online_search_request,
    public_search_capability_available,
    public_search_unavailable_message,
)
from edge_agent_egress_queue import EgressQueueError, SharedEgressQueue
from edge_agent_ingress import classify as classify_ingress, is_deliberation_request
from edge_agent_state import write_task_state
from edge_agent_team_contract import render_team_contract
from weather_adapter import fetch_weather, is_weather_request


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
HEALTH_STATE_FILE = Path(
    os.environ.get("RODA_GEMMA_HEALTH_STATE_FILE", str(HOME / ".edge-agent/state/telegram-health-monitor.json"))
).expanduser()
REQUIRE_FULL_GROUP_INTAKE = os.environ.get("EDGE_AGENT_REQUIRE_FULL_GROUP_INTAKE", "1").strip().casefold() not in {"0", "false", "no", "off"}
TELEGRAM_SINGLETON_LOCK_ROOT = Path(
    os.environ.get(
        "EDGE_AGENT_TELEGRAM_SINGLETON_LOCK_ROOT",
        str(HOME / ".claude" / "hooks-state" / "telegram-bridge-locks"),
    )
).expanduser()

RODA_IDENTITY = render_agent_profile("roda")
TEAM_CONTRACT = render_team_contract()
SYSTEM_PROMPT = (
    f"{RODA_IDENTITY}\n\n"
    "너는 Edge Agent Telegram 팀의 Roda 구성원이며, 로컬 Gemma4를 담당한다. "
    "공통 팀 계약에 적힌 다른 세 구성원을 알고 있는 상태에서 답한다. "
    "현재 역할은 짧고 정확한 대화와 간단한 안내뿐이다. "
    "주입된 스킬 문서는 안전 규칙과 판단 기준일 뿐, 이 봇에 웹 검색이나 외부 도구 권한을 추가하지 않는다. "
    "실제 조회하지 않은 출처·검색 결과·실행 결과를 만들거나 완료했다고 말하지 않는다. "
    "파일 수정, 셸 명령, 시스템 조작, 외부 전송, 계정·인증 처리는 하지 않는다. "
    "다만 4인 deliberation에서는 사용자 목표에 대한 현실성·실행 가능성 의견을 낼 수 있고, "
    "실제 실행 결과를 주장하지 않는다. "
    "확인하지 않은 실행 결과를 완료했다고 말하지 않는다."
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s roda-gemma: %(message)s")
log = logging.getLogger("roda-gemma")
for noisy_logger in ("httpx", "httpcore", "telegram", "telegram.ext"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)
REQUEST_SEMAPHORE = asyncio.Semaphore(1)
_EGRESS_QUEUE = SharedEgressQueue()
_CONTROL_PLANE = ControlPlaneStore()
_DELEGATION_TASK: asyncio.Task | None = None


def _harden_log_permissions() -> None:
    for descriptor in (1, 2):
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass


def _acquire_telegram_singleton_lock(token: str) -> int:
    """Fail before polling when another local process owns this token."""
    TELEGRAM_SINGLETON_LOCK_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(token.encode()).hexdigest()[:32]
    path = TELEGRAM_SINGLETON_LOCK_ROOT / f"singleton-{digest}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(path, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise SystemExit("다른 로컬 프로세스가 이미 이 Telegram 토큰으로 폴링 중입니다") from exc
    except BaseException:
        os.close(fd)
        raise
    return fd


async def _egress_send(chat_id: object, delivery_id: object, chunk_index: int, sender):
    for attempt in range(2):
        permit = await asyncio.to_thread(
            _EGRESS_QUEUE.acquire,
            chat_id,
            delivery_id=str(delivery_id),
            chunk_index=chunk_index,
        )
        try:
            return await sender()
        except RetryAfter as exc:
            if attempt == 1:
                raise
            await asyncio.sleep(min(float(exc.retry_after), 30.0))
        finally:
            await asyncio.to_thread(permit.release)


def _context_store() -> ContextEnvelopeStore:
    return ContextEnvelopeStore(os.environ.get("TELEGRAM_CONTEXT_ROOT") or os.environ.get("RODA_CONTEXT_ROOT") or None)


def _ollama_chat(prompt: str) -> str:
    shared_context = build_shared_context(
        prompt,
        provider="roda",
        extra_context=SYSTEM_PROMPT,
        channel="telegram",
        include_capability_preflight=False,
    )
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": shared_context},
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


def _is_unresolved_incident_query(text: str) -> bool:
    normalized = " ".join(str(text or "").split()).casefold()
    return any(word in normalized for word in ("미해결", "해결되지 않은", "남아있는", "남아 있는")) and any(
        word in normalized for word in ("오류", "장애", "문제", "에러")
    )


def _render_unresolved_incidents(path: Path | None = None) -> str:
    target = path or HEALTH_STATE_FILE
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "현재 기록된 미해결 장애가 없습니다."
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return "⚠️ 장애 원장을 읽지 못했습니다. 상태 파일 점검이 필요합니다."
    incidents = payload.get("incidents") if isinstance(payload, dict) else None
    if not isinstance(incidents, dict):
        return "⚠️ 장애 원장 형식이 오래되었거나 유효하지 않습니다. health monitor 상태 마이그레이션이 필요합니다."
    open_items = [
        item for item in incidents.values()
        if isinstance(item, dict) and item.get("status") in {"open", "reopened", "mitigated"}
    ]
    open_items.sort(key=lambda item: float(item.get("last_seen_at", 0) or 0), reverse=True)
    if not open_items:
        return "현재 장애 원장에 미해결 사건이 없습니다."
    lines = [f"현재 미해결 장애는 {len(open_items)}건입니다."]
    for item in open_items[:20]:
        task = f" task={item.get('task_id')}" if item.get("task_id") else ""
        lines.append(
            f"- {item.get('role', 'unknown')}: {item.get('code', 'unknown')} "
            f"상태={item.get('status', 'open')}{task}; incident={item.get('incident_id', '')}"
        )
    if len(open_items) > 20:
        lines.append(f"- 그 외 {len(open_items) - 20}건")
    return "\n".join(lines)


def _strip_group_mention(text: str) -> str | None:
    # This must be the same decision used by the three provider bridges.
    # The shared ingress contract decides whether this room message reaches
    # Roda. Plain group messages are room-wide; direct addresses remain
    # exclusive.
    decision = classify_ingress(text)
    if not decision.accepts("roda"):
        return None
    cleaned = re.sub(r"(?<!\w)로다(?:에게|한테|야|아|는|가|랑|과|와|도|만|님)?(?!\w)", "", decision.cleaned_text, count=1, flags=re.IGNORECASE)
    return " ".join(cleaned.split()).strip() or "안녕하세요. 무엇을 도와드릴까요?"


async def _process_delegation(application: Application, delegation: dict[str, object]) -> None:
    """Execute a Codex preprocessing assignment and publish the result."""
    store = DelegationStore()
    delegation_id = str(delegation.get("delegation_id") or "")
    request = str(delegation.get("request") or "")
    chat_id = str(delegation.get("chat_id") or "")
    reply_id = str(delegation.get("reply_to_message_id") or "").strip()
    try:
        reply_to = int(reply_id) if reply_id else None
    except ValueError:
        reply_to = None
    try:
        await application.bot.send_message(
            chat_id=chat_id,
            text="🧭 Roda가 코덱스의 자료 정리 위임을 받았습니다. 제공된 내용만 기준으로 답변하겠습니다.",
            reply_to_message_id=reply_to,
        )
    except Exception as exc:
        log.warning("delegation acknowledgement failed; continuing: %s", type(exc).__name__)
    prompt = (
        "[Codex 위임 작업]\n"
        f"위임 이유: {delegation.get('reason', '')}\n"
        f"정리 기준: {delegation.get('acceptance_criteria', '')}\n\n"
        "제공된 요청만 요약·추출·현실성 관점에서 처리하라. 웹 검색, 셸, 파일 수정, 외부 전송, "
        "확인하지 않은 실행 결과 주장은 하지 말고, 다른 에이전트에게 다시 위임하지 마라.\n\n"
        f"[원본 요청]\n{request}"
    )
    async with REQUEST_SEMAPHORE:
        try:
            answer = await asyncio.to_thread(_ollama_chat, prompt)
            sent = True
            try:
                for part in _split_message(answer):
                    await application.bot.send_message(
                        chat_id=chat_id,
                        text=part,
                        reply_to_message_id=reply_to,
                    )
            except Exception as exc:
                sent = False
                log.warning("delegation result delivery failed: %s", type(exc).__name__)
            store.complete(
                delegation_id,
                status="completed" if sent else "delivery_pending",
                response=answer,
                delivery_status="delivered" if sent else "pending",
            )
            try:
                write_task_state(
                    role="gemma",
                    chat_id=chat_id,
                    text=request,
                    status="completed" if sent else "delivery_pending",
                    task_id=delegation_id,
                    response_tail=answer[-1000:],
                    delegation_id=delegation_id,
                    workspace="local-ollama",
                )
            except (OSError, ValueError, TypeError):
                pass
        except Exception as exc:
            log.warning("delegation execution failed: %s", type(exc).__name__)
            try:
                store.complete(delegation_id, status="failed", error=f"{type(exc).__name__}: {exc}")
            except (OSError, ValueError, TypeError):
                pass
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Roda 위임 작업 실패: {type(exc).__name__}",
                    reply_to_message_id=reply_to,
                )
            except Exception:
                pass


async def _delegation_worker(application: Application) -> None:
    store = DelegationStore()
    while True:
        try:
            delegation = await asyncio.to_thread(
                store.claim_for_role,
                "roda",
                owner="telegram-roda",
            )
            if delegation is not None:
                await _process_delegation(application, delegation)
            else:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("delegation queue error; polling continues: %s", type(exc).__name__)
            await asyncio.sleep(2.0)


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
    deliberation_session_id = None
    if chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        if chat.id not in ALLOWED_GROUP_IDS:
            return
        text = _strip_group_mention(text)
        if text is None:
            return
        if is_deliberation_request(original_text) and classify_ingress(original_text).accepts("claude"):
            deliberation_session_id = session_id_for_telegram(chat.id, message.message_id)
            DeliberationStore().start(deliberation_session_id, original_text, roles=roles_for_request(original_text))
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
    if is_cancel_request(text):
        try:
            _CONTROL_PLANE.cancel_chat(chat.id, reason=text, actor="telegram-roda")
            await message.reply_text("🛑 로다 작업과 대기 중인 하위 작업을 취소했습니다.")
        except (ControlPlaneError, OSError, ValueError, TypeError) as exc:
            log.warning("control-plane cancellation failed: %s", type(exc).__name__)
            await message.reply_text("⚠️ 취소 상태를 기록하지 못했습니다. 다시 시도해 주세요.")
        return
    if _is_unresolved_incident_query(text):
        await message.reply_text(_render_unresolved_incidents())
        return
    if is_online_search_request(text) and not public_search_capability_available():
        await message.reply_text(public_search_unavailable_message())
        return
    prompt = f"{preparation.prompt_block}\n\n[사용자 요청]\n{text}" if preparation is not None else text
    if deliberation_session_id:
        prompt = "[이번 deliberation의 Roda 1차 의견: 현실성·사용자 관점]\n" + prompt

    await context.bot.send_chat_action(chat_id=chat.id, action="typing")
    task_id = ""
    try:
        task_id = write_task_state(
            role="gemma", chat_id=chat.id, text=original_text, status="started",
            workspace="local-ollama", auth_source="telegram-token-file",
        )
    except (OSError, ValueError, TypeError) as exc:
        log.warning("coordination state start failed: %s", type(exc).__name__)
    if task_id:
        try:
            _CONTROL_PLANE.start_task(chat.id, task_id, roles=tuple(roles_for_request(original_text)))
        except (ControlPlaneError, OSError, ValueError, TypeError) as exc:
            log.warning("control-plane task start failed: %s", type(exc).__name__)
    async with REQUEST_SEMAPHORE:
        log.info("request started chat=%s", chat.id)
        try:
            if is_weather_request(text):
                try:
                    answer = (await asyncio.to_thread(fetch_weather, text)).as_text()
                except Exception as weather_exc:
                    log.warning("weather lookup failed; refusing an unverified model guess: %s", type(weather_exc).__name__)
                    answer = "⚠️ 실시간 날씨 조회에 실패했습니다. 잠시 후 다시 요청해 주세요."
            else:
                answer = await asyncio.to_thread(_ollama_chat, prompt)
        except Exception as exc:  # keep Telegram polling alive on provider errors
            log.warning("request failed chat=%s: %s", chat.id, exc)
            try:
                write_task_state(role="gemma", chat_id=chat.id, text=original_text, status="failed", task_id=task_id, error=str(exc)[-500:])
            except (OSError, ValueError, TypeError):
                pass
            if task_id:
                try:
                    _CONTROL_PLANE.mark_task(chat.id, task_id, "failed", summary=str(exc))
                except (ControlPlaneError, OSError, ValueError, TypeError):
                    pass
            await message.reply_text("지금은 Gemma4에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.")
            return
    if deliberation_session_id:
        store = DeliberationStore()
        store.record(deliberation_session_id, "roda", status="completed", summary=answer)
        try:
            await asyncio.to_thread(
                store.wait_for_round,
                deliberation_session_id,
                1,
                timeout_seconds=configured_barrier_timeout_seconds(),
            )
            if store.round_state(deliberation_session_id, 1) != "ready":
                raise RuntimeError("deliberation round 1 barrier not ready")
            follow_up = (
                "[peer follow-up 단계]\n"
                "다른 역할의 서명된 peer evidence를 검토하고, 반론·보완점·현실적인 실행 단계를 반영해 다시 답하라.\n\n"
                f"{original_text}\n\n{store.render(deliberation_session_id, consumer_role="roda")}"
            )
            answer = await asyncio.to_thread(_ollama_chat, follow_up)
            store.record(deliberation_session_id, "roda", status="completed", summary=answer, round_number=2)
            if store.max_rounds(deliberation_session_id) >= 3:
                await asyncio.to_thread(
                    store.wait_for_round,
                    deliberation_session_id,
                    2,
                    timeout_seconds=configured_barrier_timeout_seconds(),
                )
                if store.round_state(deliberation_session_id, 2) != "ready":
                    raise RuntimeError("deliberation round 2 barrier not ready")
                adjudication = (
                    "[최종 adjudication 단계]\n"
                    "1·2차 peer evidence의 합의와 충돌을 비교하고, 불확실성은 명시한 최종안을 작성하라.\n\n"
                    f"{original_text}\n\n{store.render(deliberation_session_id, consumer_role="roda")}"
                )
                answer = await asyncio.to_thread(_ollama_chat, adjudication)
                store.record(deliberation_session_id, "roda", status="completed", summary=answer, round_number=3)
        except Exception as exc:
            log.warning("peer follow-up failed; withholding final deliberation answer: %s", type(exc).__name__)
            try:
                store.record(
                    deliberation_session_id,
                    "roda",
                    status="failed",
                    summary=f"{type(exc).__name__}: {exc}",
                    round_number=2,
                )
            except (OSError, ValueError, TypeError):
                pass
            answer = "⚠️ 네 역할 모두의 서명된 논의 결과가 모이지 않아 최종안을 보류합니다. 실패 원인을 확인한 뒤 다시 시도해 주세요."
    if should_publish_user_result("roda", deliberation_session_id):
        try:
            for index, part in enumerate(_split_message(answer)):
                sent = await _egress_send(
                    chat.id,
                    task_id,
                    index,
                    lambda: message.reply_text(part),
                )
                if preparation is not None and preparation.resolution.anchor is not None and getattr(sent, "message_id", None) is not None:
                    _context_store().bind_response_message(
                        channel="telegram",
                        chat_id=chat.id,
                        source_message_id=preparation.resolution.anchor.source_message_id,
                        response_message_id=sent.message_id,
                    )
        except EgressQueueError as exc:
            log.warning("egress queue backpressure chat=%s: %s", chat.id, type(exc).__name__)
            try:
                write_task_state(role="gemma", chat_id=chat.id, text=original_text, status="delivery_pending", task_id=task_id, response_tail=answer[-1000:], error=str(exc))
            except (OSError, ValueError, TypeError):
                pass
            return
    try:
        write_task_state(
            role="gemma", chat_id=chat.id, text=original_text, status="completed", task_id=task_id,
            response_tail=answer[-1000:], workspace="local-ollama", auth_source="telegram-token-file",
        )
    except (OSError, ValueError, TypeError) as exc:
        log.warning("coordination state completion failed: %s", type(exc).__name__)
    if task_id:
        try:
            _CONTROL_PLANE.mark_task(chat.id, task_id, "completed", summary=answer)
        except (ControlPlaneError, OSError, ValueError, TypeError) as exc:
            log.warning("control-plane task completion failed: %s", type(exc).__name__)
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
                message = "group=%s privacy mode may filter unmentioned text; use @%s or promote the bot for full group intake"
                if REQUIRE_FULL_GROUP_INTAKE:
                    raise RuntimeError(message % (group_id, actual))
                log.warning(message, group_id, actual)
        except Exception as exc:  # keep polling alive; health is still logged
            if REQUIRE_FULL_GROUP_INTAKE:
                log.error("group=%s privacy/intake verification failed: %s", group_id, type(exc).__name__)
                application.stop_running()
                os._exit(1)
            log.warning("group=%s membership check unavailable: %s", group_id, type(exc).__name__)
    global _DELEGATION_TASK
    # post_init runs before PTB marks the Application as running. Own the
    # worker explicitly so restarts do not leave a pending task behind.
    _DELEGATION_TASK = asyncio.get_running_loop().create_task(
        _delegation_worker(application),
        name="edge-agent-delegation-roda",
    )
    log.info("Codex 위임 큐 소비자 시작 role=roda")


async def post_shutdown(application: Application) -> None:
    """Cancel the delegation worker before PTB closes the event loop."""
    del application
    global _DELEGATION_TASK
    task = _DELEGATION_TASK
    _DELEGATION_TASK = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def main() -> None:
    _harden_log_permissions()
    if not TOKEN_FILE.is_file():
        raise SystemExit(f"토큰 파일이 없습니다: {TOKEN_FILE}")
    if TOKEN_FILE.stat().st_mode & 0o077:
        raise SystemExit("토큰 파일 권한이 안전하지 않습니다.")
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("토큰 파일이 비어 있습니다.")
    _acquire_telegram_singleton_lock(token)
    # Python 3.14 no longer creates the main event loop implicitly, while
    # python-telegram-bot's run_polling() still expects one to exist.
    asyncio.set_event_loop(asyncio.new_event_loop())
    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(TypeHandler(Update, log_update_receipt), group=-1)
    application.add_handler(MessageHandler(filters.TEXT | filters.CaptionRegex("."), handle_message))
    application.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
