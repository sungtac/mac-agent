"""Small shared-result collector for explicit Telegram coordination requests."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from edge_agent_state import list_task_states
from edge_agent_peer_context import _launchctl_state


PEER_ROLES = ("antigravity", "gemma")
TERMINAL_STATUSES = {"completed", "failed", "delivery_pending"}
SERVICE_LABELS = {
    "antigravity": "com.macagent.telegram-antigravity",
    "gemma": "com.macagent.telegram-roda-gemma",
}
STATUS_QUERY_MARKERS = ("현재 상태", "서비스 상태", "실행 상태", "상태 확인", "정상인지", "헬스체크", "health check")


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").split())


def peer_roles_for_request(request: str) -> tuple[str, ...]:
    """Select only peers explicitly named in a coordination request."""
    normalized = _normalize(request)
    roles: list[str] = []
    if re.search(r"(?:안티|안티그래비티)", normalized):
        roles.append("antigravity")
    if re.search(r"로다|gemma", normalized, re.IGNORECASE):
        roles.append("gemma")
    return tuple(roles) or PEER_ROLES


def _is_status_request(request: str) -> bool:
    normalized = _normalize(request).casefold()
    return any(marker.casefold() in normalized for marker in STATUS_QUERY_MARKERS)


def _latest_matching(*, role: str, chat_id: object, request: str) -> dict[str, Any] | None:
    normalized = _normalize(request)
    for record in list_task_states(role=role, chat_id=str(chat_id)):
        if str(record.get("error") or "").strip() == "Ollama must not run":
            continue
        if _normalize(record.get("request_tail") or record.get("request_preview")) == normalized:
            return record
    return None


def collect_report(*, chat_id: object, request: str, roles: tuple[str, ...] | None = None) -> str:
    """Render bounded evidence for peer work matching the same request."""
    selected_roles = roles or peer_roles_for_request(request)
    lines = ["[Peer result packet: shared-state observation]"]
    for role in selected_roles:
        record = _latest_matching(role=role, chat_id=chat_id, request=request)
        service = _launchctl_state(SERVICE_LABELS[role])
        if not record:
            lines.append(f"- {role}: service={service}; task=not_observed; response is not confirmed")
            continue
        status = _normalize(record.get("status")) or "unknown"
        updated = _normalize(record.get("updated_at")) or "unknown"
        if _is_status_request(request):
            lines.append(
                f"- {role}: service={service}; task={status}; updated={updated}; "
                "historical response omitted; service state is the authoritative status evidence"
            )
            continue
        response = _normalize(record.get("response_tail") or record.get("response_preview"))
        if len(response) > 700:
            response = response[-700:]
        line = f"- {role}: status={status}; updated={updated}"
        if response:
            line += f"; response={response}"
        lines.append(line)
    lines.append("Only terminal peer states are completed evidence; not_observed/started states require follow-up.")
    return "\n".join(lines)[:2600]


async def wait_for_peer_results(
    *, chat_id: object, request: str, roles: tuple[str, ...] | None = None,
    timeout_seconds: float = 8.0, interval_seconds: float = 0.5,
) -> str:
    """Wait briefly for terminal peer states, then return an honest report."""
    selected_roles = roles or peer_roles_for_request(request)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        records = [_latest_matching(role=role, chat_id=chat_id, request=request) for role in selected_roles]
        if all(record and str(record.get("status")) in TERMINAL_STATUSES for record in records):
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(max(0.05, interval_seconds))
    return collect_report(chat_id=chat_id, request=request, roles=selected_roles)


__all__ = ["collect_report", "peer_roles_for_request", "wait_for_peer_results"]
