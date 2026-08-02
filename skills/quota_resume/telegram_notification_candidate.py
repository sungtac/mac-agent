#!/usr/bin/env python3
"""Build dry-run Telegram notification candidates without sending anything."""
from __future__ import annotations

from datetime import datetime
from typing import Any

DELIVERY_CLAIMS = ["전송 완료", "발송 완료", "보냈습니다", "sent successfully"]


def build_notification_bundle(taskbrief: dict[str, Any]) -> dict[str, Any]:
    task_id = str(taskbrief.get("task_id") or "UNKNOWN_TASK")
    objective = str(taskbrief.get("objective") or "재개 후보 작업")
    steps = taskbrief.get("next_safe_steps") or taskbrief.get("remaining_steps") or []
    if not isinstance(steps, list):
        steps = [str(steps)]
    message = "\n".join([
        "작업 재개 후보가 준비됐습니다.",
        f"- task_id: {task_id}",
        f"- objective: {objective}",
        "- auto_execute: false",
        "- requires_user_review: true",
        "- next_safe_steps: " + (", ".join(str(step) for step in steps) if steps else "확인 필요"),
        "※ 아직 전송/실행 완료가 아니라 검토용 후보입니다.",
    ])
    return {
        "timestamp": datetime.now().isoformat(),
        "messages": [{
            "task_id": task_id,
            "message_candidate": message,
            "delivery_success": False,
            "message_id": None,
        }],
    }


def assert_no_delivery_claim_without_message_id(message: dict[str, Any]) -> dict[str, Any]:
    text = str(message.get("message_candidate") or "")
    has_message_id = bool(message.get("message_id"))
    detected = [claim for claim in DELIVERY_CLAIMS if claim.lower() in text.lower()]
    return {"ok": not (detected and not has_message_id), "detected": detected, "has_message_id": has_message_id}
