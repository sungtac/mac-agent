"""Bounded, read-only context about the other agent bridges.

This is coordination context, not proof that another provider completed a
task. It deliberately exposes service/task observations only and never reads
Telegram tokens or provider credentials.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any

from edge_agent_state import latest_task_state


CHAT_ID = "-1003952617795"
MAX_SNAPSHOT_CHARS = 2600
MAX_FIELD_CHARS = 220
PEERS: tuple[tuple[str, str, str], ...] = (
    ("claude", "com.macagent.telegram-claude", "claude"),
    ("codex", "com.multiagent.engine", "codex"),
    ("antigravity", "com.macagent.telegram-antigravity", "antigravity"),
    ("roda", "com.macagent.telegram-roda-gemma", "gemma"),
)


def _launchctl_state(label: str) -> str:
    try:
        result = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if result.returncode != 0:
        return "not_running"
    output = f"{result.stdout}\n{result.stderr}".casefold()
    return "running" if "state = running" in output or "pid = " in output else "unknown"


def _clip(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[-limit:]


def _task_summary(role: str) -> str:
    try:
        record = latest_task_state(role=role, chat_id=CHAT_ID)
    except (OSError, ValueError, TypeError):
        return "task=unknown"
    if not record:
        return "task=none"
    if str(record.get("error") or "").strip() == "Ollama must not run":
        return "activity=idle; last_task=not_observed; synthetic_test_record_ignored"
    status = _clip(record.get("status"), 80) or "unknown"
    try:
        age = time.time() - float(record.get("updated_epoch") or 0)
    except (TypeError, ValueError):
        age = None
    active = status in {"started", "running", "waiting"} and (age is None or age <= 600)
    parts = [
        f"activity={'active' if active else 'idle'}",
        f"last_task={status}",
        f"updated={_clip(record.get('updated_at'), 40) or 'unknown'}",
    ]
    parts.append("historical_response=omitted; use a request-matched result packet for evidence")
    return "; ".join(parts)


def snapshot(*, max_chars: int = MAX_SNAPSHOT_CHARS) -> str:
    """Return a bounded peer snapshot suitable for provider prompts."""
    lines = [
        "[Peer coordination snapshot: read-only observation]",
        "This describes local bridge state; it is not proof that a peer executed or approved anything.",
    ]
    for name, label, task_role in PEERS:
        lines.append(f"- {name}: service={_launchctl_state(label)}; {_task_summary(task_role)}")
    lines.append("Explicitly address the intended peer and require evidence before treating its work as complete.")
    return "\n".join(lines)[:max(200, int(max_chars))]


__all__ = ["snapshot"]


if __name__ == "__main__":
    print(snapshot())
