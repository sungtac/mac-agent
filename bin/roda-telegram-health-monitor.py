#!/usr/bin/env python3
"""Detect Telegram provider failures and send deduplicated Roda alerts."""

from __future__ import annotations

import hashlib
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from edge_agent_parallel_locks import integration_lock, repository_lifecycle_lock
except ImportError:  # system-Python test loader; launchd uses the repo bin path
    integration_lock = None
    repository_lifecycle_lock = None


HOME = Path.home()
STATE_FILE = Path(os.environ.get("RODA_GEMMA_HEALTH_STATE_FILE", "~/.edge-agent/state/telegram-health-monitor.json")).expanduser()
TOKEN_FILE = Path(os.environ.get("RODA_GEMMA_TOKEN_FILE", "~/.edge-agent/secrets/roda-gemma/telegram.token")).expanduser()
POLL_SECONDS = int(os.environ.get("RODA_GEMMA_HEALTH_POLL_SECONDS", "30"))
NO_RESPONSE_SECONDS = int(os.environ.get("RODA_GEMMA_HEALTH_NO_RESPONSE_SECONDS", "300"))
SERVICE_DOWN_GRACE_SECONDS = int(os.environ.get("RODA_GEMMA_SERVICE_DOWN_GRACE_SECONDS", "90"))
MAINTENANCE_FILE = Path(
    os.environ.get(
        "EDGE_AGENT_TELEGRAM_MAINTENANCE_FILE",
        str(HOME / ".edge-agent" / "state" / "telegram-maintenance.json"),
    )
).expanduser()
RECOVERY_TIMEOUT_SECONDS = int(os.environ.get("RODA_GEMMA_HEALTH_RECOVERY_TIMEOUT_SECONDS", "300"))
USAGE_WATCH_TTL_SECONDS = int(os.environ.get("RODA_GEMMA_USAGE_WATCH_TTL_SECONDS", str(24 * 60 * 60)))
USAGE_WATCH_GRACE_SECONDS = int(os.environ.get("RODA_GEMMA_USAGE_WATCH_GRACE_SECONDS", "3600"))
# A repair commit that can't merge yet (main dirty, transient conflict) is
# queued and retried every cycle instead of being dropped on the floor — see
# docs/roda-parallel-recovery-improvement-plan.md P2.
PENDING_MERGE_TTL_SECONDS = int(os.environ.get("RODA_GEMMA_PENDING_MERGE_TTL_SECONDS", str(24 * 60 * 60)))
MAIN_DIRTY_ALERT_INTERVAL_SECONDS = int(os.environ.get("RODA_GEMMA_MAIN_DIRTY_ALERT_INTERVAL_SECONDS", str(24 * 60 * 60)))
STATE_SCHEMA_VERSION = 6
ALERT_RETENTION_SECONDS = int(os.environ.get("RODA_GEMMA_ALERT_RETENTION_SECONDS", str(7 * 24 * 60 * 60)))
# Stage-1..3 escalation reuses the existing timing constants verbatim (design
# doc 2026-08-16-roda-role-escalation-design.md, 확정 사항 1-3) — these are
# aliases, not new tunables.
ROUTING_ACK_TIMEOUT_SECONDS = NO_RESPONSE_SECONDS
ROUTING_COMPLETION_TIMEOUT_SECONDS = PENDING_MERGE_TTL_SECONDS
INCIDENT_MERGE_WINDOW_SECONDS = NO_RESPONSE_SECONDS
REPEAT_FAILURE_WINDOW_SECONDS = ALERT_RETENTION_SECONDS
DRY_RUN = os.environ.get("RODA_GEMMA_HEALTH_DRY_RUN", "0") == "1"
CODEX_DIAGNOSIS_ENABLED = os.environ.get("RODA_GEMMA_CODEX_DIAGNOSIS_ENABLED", "1") == "1"
# Automatic provider-authored changes are opt-in.  Diagnosis and alerting can
# remain enabled without granting the health monitor commit/merge/restart
# authority.  This default is deliberately fail-closed for a long-running
# launchd service.
AUTO_REPAIR_ENABLED = os.environ.get("RODA_GEMMA_AUTO_REPAIR_ENABLED", "0") == "1"
AUTO_REPAIR_APPROVAL_FILE = Path(
    os.environ.get(
        "RODA_GEMMA_AUTO_REPAIR_APPROVAL_FILE",
        "~/.edge-agent/state/roda-auto-repair-approvals.json",
    )
).expanduser()
CODEX_BIN = Path(os.environ.get("RODA_GEMMA_CODEX_BIN", "/opt/homebrew/bin/codex"))
SOURCE_REPO = Path(os.environ.get("RODA_GEMMA_CODEX_SOURCE_REPO", "~/mac-agent")).expanduser().resolve()
DIAGNOSIS_ROOT = Path(os.environ.get("RODA_GEMMA_CODEX_DIAGNOSIS_ROOT", "~/.edge-agent-worktrees/health-diagnoses")).expanduser().resolve()
REPAIR_ROOT = Path(os.environ.get("RODA_GEMMA_CODEX_REPAIR_ROOT", "~/.edge-agent-worktrees/health-repairs")).expanduser().resolve()
BOT_USERNAMES = {"claude": "edgeai_stk_bot", "codex": "edgeai_macmini_bot", "antigravity": "edgeai_anti_bot"}
ALERT_GROUP_IDS = tuple(
    int(value.strip())
    for value in os.environ.get("RODA_GEMMA_ALERT_GROUP_IDS", "-1003952617795").split(",")
    if value.strip()
)

TARGETS = {
    "claude": {
        "label": "com.macagent.telegram-claude",
        "log": HOME / ".claude/hooks-state/telegram-claude/stderr.log",
    },
    "codex": {
        "label": "com.multiagent.engine",
        "log": HOME / ".edge-agent/state/multiagent-engine/stderr.log",
    },
    "antigravity": {
        "label": "com.macagent.telegram-antigravity",
        "log": HOME / ".claude/hooks-state/telegram-antigravity/stderr.log",
    },
}
RESTART_HELPER = Path(__file__).with_name("edge-agent-telegram-restart.py")
LOCAL_TIMEZONE = ZoneInfo("Asia/Seoul")

ERROR_PATTERNS = (
    ("empty_response", re.compile(r"빈 응답|empty response", re.I)),
    ("execution_error", re.compile(r"실행 오류|실행에 실패|처리 실패|exit=\d+", re.I)),
)
# A deliberation barrier failure is a request-level coordination outcome, not
# proof that the role named in the log is unhealthy. The deputy coordinator
# already consumes the durable peer results and publishes a bounded fallback.
DELIBERATION_BARRIER_CONTROL_RE = re.compile(
    r"\berror=deliberation round \d+ barrier (?:failed|collecting)\b",
    re.I,
)
# Usage exhaustion is an operational state, not a code defect. Keep these
# patterns provider-neutral because Claude, Codex and Antigravity expose
# different wording for the same condition.
USAGE_LIMIT_RE = re.compile(
    r"hit your (?:session|usage) limit|(?:usage|session) limit\s*(?:reached|exceeded|exhausted)|"
    r"you have reached your (?:session|usage) limit|usage cap|quota\s*(?:exceeded|reached|exhausted)|"
    r"(?:limit|quota)\s*(?:exceeded|reached|exhausted)|resource_exhausted|"
    r"baseline quota.*(?:reached|exhausted)|사용량\s*(?:제한|한도)|"
    r"쿼터\s*(?:초과|소진)",
    re.I,
)
# Claude's "session limit" is scoped to the resumable native CLI session. It
# is not proof that the account-wide subscription allowance is exhausted.
# Keep it as a separate signal so Roda cannot turn one stderr line into a
# false account-usage report.
SESSION_LOCAL_LIMIT_RE = re.compile(
    r"hit\s+your\s+session\s+limit|session\s+limit(?:\s+(?:reached|exceeded|exhausted))?",
    re.I,
)
EXPLICIT_USAGE_LIMIT_RE = re.compile(
    r"hit your (?:session|usage) limit|(?:usage|session) limit\s*(?:reached|exceeded|exhausted)|usage cap|"
    r"you have reached your (?:session|usage) limit|quota\s*(?:exceeded|reached|exhausted)|"
    r"(?:limit|quota)\s*(?:exceeded|reached|exhausted)|resource_exhausted|"
    r"baseline quota.*(?:reached|exhausted)|사용량\s*(?:제한|한도)|쿼터\s*(?:초과|소진)",
    re.I,
)
QUOTA_EVIDENCE_RE = re.compile(
    r"usage\s+(?:cap|limit)|session\s+limit|quota|baseline quota|사용량|쿼터",
    re.I,
)
RATE_LIMIT_RE = re.compile(
    r"rate[_ -]?limit(?:_error)?|too many requests|HTTP\s*429|status code\s*:?\s*429|"
    r"\b429\b|retry[- _]?after|throttl",
    re.I,
)
CONTEXT_LIMIT_RE = re.compile(
    r"context(?: window| length)?\s*(?:limit|exceeded|too long)|maximum context length|"
    r"context window is full|컨텍스트\s*(?:초과|한도)",
    re.I,
)
CAPACITY_LIMIT_RE = re.compile(
    r"resource[_ -]?exhausted.*(?:capacity|overload|temporarily unavailable)|"
    r"capacity\s+(?:limit|exhausted|unavailable)|temporarily unavailable|try again later",
    re.I,
)
SERVICE_OVERLOAD_RE = re.compile(r"overloaded_error|service\s+overloaded|server\s+overloaded|\b529\b", re.I)
AUTH_ERROR_RE = re.compile(
    r"authentication_error|unauthorized|invalid api key|permission denied|"
    r"failed to authenticate|oauth session expired|could not be refreshed|"
    r"인증[^\n]{0,40}(?:만료|실패)|\b401\b|\b403\b",
    re.I,
)
FAILURE_CONTEXT_RE = re.compile(r"error|fail|실패|오류|exit\s*=|status code|HTTP", re.I)
EXPLICIT_RATE_LIMIT_RE = re.compile(
    r"rate[_ -]?limit(?:_error)|rate[_ -]?limit(?:s)?\s+(?:exceeded|reached)|"
    r"too many requests|HTTP\s*429|status code\s*:?\s*429|\b429\b",
    re.I,
)
NON_REPAIRABLE_CODES = frozenset({
    "usage_limited",
    "session_limited",
    "rate_limited",
    "capacity_limited",
    "service_overloaded",
    "context_exceeded",
    "auth_error",
    "main_dirty",
})
RESOURCE_RECOVERY_CODES = frozenset({"usage_limited", "session_limited", "rate_limited", "capacity_limited", "service_overloaded"})
UNKNOWN_SIGNAL_RE = re.compile(
    r"provider|quota|usage\s+(?:cap|limit)|resource[_ -]?exhausted|rate[_ -]?limit|"
    r"429|capacity|overload|unavailable|retry[- _]?after",
    re.I,
)
STRUCTURED_PROVIDER_TYPES = frozenset({
    "rate_limit_error",
    "rate_limited",
    "authentication_error",
    "permission_error",
    "unauthorized",
    "overloaded_error",
    "service_overloaded",
    "context_exceeded",
    "request_too_large",
    "usage_limit",
    "usage_limited",
    "quota_exceeded",
    "resource_exhausted",
})
STRUCTURED_PROVIDER_STATUSES = frozenset({"401", "403", "429", "503", "529", "too_many_requests", "resource_exhausted"})
USAGE_WINDOW_RE = re.compile(
    r"(?P<window>5\s*[- ]?(?:h|hours?)|five\s+hours?|7\s*[- ]?(?:d|days?)|"
    r"seven\s+days?|weekly|monthly)",
    re.I,
)
RESET_AFTER_RE = re.compile(
    r"(?:retry[- _]?after|resets?\s+in)\s*[:=]?\s*"
    r"(?P<duration>\d+(?:\.\d+)?\s*(?:seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)?"
    r"(?:\s*(?:(?:and|,)\s*)?\d+(?:\.\d+)?\s*(?:seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)?){0,3})",
    re.I,
)
DURATION_PART_RE = re.compile(
    r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)?",
    re.I,
)
RESET_AT_RE = re.compile(
    r"(?:resets?|reset\s+at|available\s+at)\D*"
    r"(?P<value>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)",
    re.I,
)
# This code was emitted by older monitor versions for an intentional
# ``stop_running()``/launchd restart cycle. Do not resurrect it from a state
# file after upgrading to the lifecycle-only handling above.
IGNORED_RETRY_CODES = frozenset({"polling_stopped"})
# ``run_polling 종료`` is emitted after the bot deliberately calls
# ``stop_running()`` (for example after a Conflict burst) so launchd's
# KeepAlive can restart it. It is a lifecycle message, not proof that the
# service stayed down; persistent failure is covered by ``service_down``.
START_RE = re.compile(r"처리 시작")
# A provider can terminate before the outer handler gets a chance to emit
# "처리 실패". The provider exit line is terminal evidence too; otherwise a
# failed CLI can remain pending until the no-response timeout.
DONE_RE = re.compile(r"처리 완료|처리 실패|exit=\d+|empty response")
TASK_ID_RE = re.compile(r"\btask=(?P<task>[A-Za-z0-9._:-]{1,180})\b")


def _line_task_id(line: str) -> str:
    match = TASK_ID_RE.search(line or "")
    return match.group("task") if match else ""


def _harden_log_permissions() -> None:
    for descriptor in (1, 2):
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass


def _default_state() -> dict:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "initialized": False,
        "offsets": {},
        "pending": {},
        "alerted": {},
        "delivery_retry": [],
        "repair_results": {},
        "recovery_watch": {},
        "usage_watch": {},
        "pending_merges": {},
        "incidents": {},
        "deliberation_history": [],
        "main_dirty_alerted_at": 0,
        "metrics": {
            "classified": {},
            "unknown": 0,
            "parse_failures": 0,
            "diagnostic_lines": 0,
            "last_event_at": None,
        },
    }


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)
    os.chmod(path, 0o600)


def _migrate_state(payload: object) -> dict:
    state = payload if isinstance(payload, dict) else {}
    defaults = _default_state()
    for key, value in defaults.items():
        if key not in state:
            state[key] = value.copy() if isinstance(value, dict) else value
    state["schema_version"] = STATE_SCHEMA_VERSION
    for key in ("offsets", "pending", "alerted", "repair_results", "recovery_watch", "usage_watch", "pending_merges", "incidents"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    if not isinstance(state.get("deliberation_history"), list):
        state["deliberation_history"] = []
    # v3 stored pending starts as a FIFO list per role.  Keep those entries
    # visible as legacy incidents, but all new observations are keyed by the
    # task ID emitted by the provider bridge so an unrelated DONE cannot pop
    # an older request.
    for role, pending in list(state["pending"].items()):
        if isinstance(pending, list):
            migrated: dict[str, float] = {}
            for index, value in enumerate(pending):
                try:
                    started = float(value)
                except (TypeError, ValueError):
                    continue
                migrated[f"legacy-{role}-{index}-{int(started)}"] = started
            state["pending"][role] = migrated
        elif not isinstance(pending, dict):
            state["pending"][role] = {}
    # Legacy FIFO starts cannot be correlated to a real task. Keeping them
    # open lets unrelated modern completions poison no_response forever.
    # Preserve their incident history as superseded, but remove them from the
    # active pending set.
    for role, pending in state["pending"].items():
        for pending_id in [item for item in pending if str(item).startswith("legacy-")]:
            pending.pop(pending_id, None)
            for incident in state.get("incidents", {}).values():
                if incident.get("role") == role and incident.get("task_id") == pending_id and incident.get("status") in {"open", "reopened"}:
                    incident["status"] = "superseded"
                    incident["resolved_at"] = time.time()
                    incident["resolution"] = "uncorrelated legacy FIFO entry retired during v4 reconciliation"
    if not isinstance(state.get("delivery_retry"), list):
        state["delivery_retry"] = []
    for fingerprint, usage in list(state.get("usage_watch", {}).items()):
        if not isinstance(usage, dict):
            state["usage_watch"].pop(fingerprint, None)
            continue
        if "expires_at" not in usage:
            try:
                created_at = float(usage.get("created_at", time.time()))
            except (TypeError, ValueError):
                created_at = time.time()
            usage["expires_at"] = created_at + USAGE_WATCH_TTL_SECONDS
        usage.setdefault("windows", [])
        usage.setdefault("recovery_confidence", "unknown")
        usage.setdefault("source", "stderr")
        usage.setdefault("authority", "local_log")
        usage.setdefault("evidence", [])
    if not isinstance(state.get("metrics"), dict):
        state["metrics"] = {}
    if not isinstance(state["metrics"].get("classified"), dict):
        state["metrics"]["classified"] = {}
    state["metrics"].setdefault("unknown", 0)
    state["metrics"].setdefault("parse_failures", 0)
    state["metrics"].setdefault("diagnostic_lines", 0)
    state["metrics"].setdefault("classified", {})
    state["metrics"].setdefault("last_event_at", None)
    for key in ("unknown", "parse_failures", "diagnostic_lines"):
        try:
            state["metrics"][key] = int(state["metrics"][key])
        except (TypeError, ValueError):
            state["metrics"][key] = 0
    for incident in state.get("incidents", {}).values():
        incident.setdefault("escalation_stage", None)
    _coalesce_specific_incidents(state)
    return state


def _load_state() -> dict:
    try:
        return _migrate_state(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _default_state()


def _save_state(state: dict) -> None:
    _atomic_write(STATE_FILE, state)


def _json_object(line: str) -> dict | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(line):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(line[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _structured_error(line: str) -> dict | None:
    payload = _json_object(line)
    if not payload:
        return None
    error = payload.get("error")
    if isinstance(error, dict) and any(error.get(key) is not None for key in ("type", "status", "code", "status_code")):
        return payload
    error_type = str(payload.get("error_type", "")).lower()
    top_level_type = str(payload.get("type", "")).lower()
    status = str(payload.get("status", payload.get("code", payload.get("status_code", "")))).lower()
    if error_type or top_level_type in STRUCTURED_PROVIDER_TYPES or top_level_type.endswith("_error"):
        return payload
    if status in STRUCTURED_PROVIDER_STATUSES:
        return payload
    if any(payload.get(key) is not None for key in ("retry_after", "retryAfter", "reset_at", "resetAt")):
        return payload
    return None


def _structured_error_type(payload: dict | None) -> str | None:
    if not payload:
        return None
    error = payload.get("error")
    if isinstance(error, dict) and error.get("type"):
        return str(error["type"])
    if payload.get("error_type"):
        return str(payload["error_type"])
    if payload.get("type") and str(payload["type"]).endswith("_error"):
        return str(payload["type"])
    return None


def _structured_error_status(payload: dict | None) -> str | None:
    if not payload:
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("status", "code", "status_code"):
            if error.get(key) is not None:
                return str(error[key])
    for key in ("status", "code", "status_code"):
        if payload.get(key) is not None:
            return str(payload[key])
    return None


def _structured_category(payload: dict | None, role: str | None) -> str | None:
    error_type = (_structured_error_type(payload) or "").lower()
    status = (_structured_error_status(payload) or "").lower()
    encoded = json.dumps(payload or {}, ensure_ascii=False).lower()
    if error_type in {"rate_limit_error", "rate_limited"} or status in {"429", "too_many_requests"}:
        return "rate_limited"
    if error_type in {"authentication_error", "permission_error", "unauthorized"} or status in {"401", "403"}:
        return "auth_error"
    if error_type in {"overloaded_error", "service_overloaded"} or status in {"529", "503"}:
        return "service_overloaded"
    if error_type in {"context_exceeded", "request_too_large"}:
        return "context_exceeded"
    if role == "antigravity" and ("resource_exhausted" in encoded or "quota" in encoded):
        return "usage_limited" if "quota" in encoded or "usage" in encoded else "capacity_limited"
    if role == "codex" and (error_type in {"usage_limit", "usage_limited", "quota_exceeded"} or re.search(r"usage|quota", encoded)):
        return "usage_limited"
    if role == "claude" and "rate_limit" in encoded:
        return "rate_limited"
    return None


def _authority_for(source: str, confidence: str, reset_source: str | None, windows: list[str]) -> str:
    if source == "structured_error":
        return "provider_response"
    if confidence == "estimated" and reset_source is None and windows:
        return "provider_policy"
    return "local_log"


def classify_line(line: str, *, role: str | None = None) -> str | None:
    prompt_marker = re.search(r"\btext\s*=", line, re.I)
    provider_prefix = line[:prompt_marker.start()] if prompt_marker else line
    prompt_only = bool(prompt_marker and not FAILURE_CONTEXT_RE.search(provider_prefix))
    json_payload = _json_object(line) if not prompt_only else None
    payload = _structured_error(line) if not prompt_only else None
    structured_category = _structured_category(payload, role)
    if structured_category:
        return structured_category
    # Once a provider error envelope has been recognized, an unknown error
    # type is diagnostic data—not permission to search arbitrary nested
    # messages for quota/usage keywords. Keep it available to the metrics
    # recorder as a parse failure and fail closed for alert classification.
    if payload is not None:
        return None
    # A JSON object without a provider diagnostic envelope is untrusted input.
    # Do not let words inside arbitrary user payloads trigger provider alerts;
    # only a clear error prefix may opt it into regex fallback.
    if json_payload is not None and payload is None:
        json_start = line.find("{")
        if not FAILURE_CONTEXT_RE.search(line[:json_start]):
            return None
    if prompt_only:
        usage_candidate = None
        rate_candidate = None
    else:
        usage_candidate = USAGE_LIMIT_RE.search(line)
        rate_candidate = RATE_LIMIT_RE.search(line)
    if prompt_only:
        return None
    if DELIBERATION_BARRIER_CONTROL_RE.search(line):
        return None
    if AUTH_ERROR_RE.search(line):
        return "auth_error"
    if SERVICE_OVERLOAD_RE.search(line):
        return "service_overloaded"
    if CONTEXT_LIMIT_RE.search(line):
        return "context_exceeded"
    if CAPACITY_LIMIT_RE.search(line) and not QUOTA_EVIDENCE_RE.search(line):
        return "capacity_limited"
    if rate_candidate and (EXPLICIT_RATE_LIMIT_RE.search(line) or FAILURE_CONTEXT_RE.search(provider_prefix)):
        return "rate_limited"
    if SESSION_LOCAL_LIMIT_RE.search(line):
        return "session_limited"
    if usage_candidate and (EXPLICIT_USAGE_LIMIT_RE.search(line) or FAILURE_CONTEXT_RE.search(provider_prefix)):
        return "usage_limited"
    if re.search(r"resource[_ -]?exhausted", line, re.I):
        return "capacity_limited"
    for code, pattern in ERROR_PATTERNS:
        if pattern.search(line):
            return code
    return None


def _duration_seconds(amount: str, unit: str | None) -> float:
    value = float(amount)
    normalized = (unit or "s").lower()
    if normalized.startswith("h"):
        return value * 3600
    if normalized.startswith("m"):
        return value * 60
    return value


def _parse_duration(text: str) -> float | None:
    parts = list(DURATION_PART_RE.finditer(text))
    if not parts:
        return None
    consumed = "".join(match.group(0) for match in parts)
    if re.sub(r"[\s,]+|and", "", consumed.lower()) != re.sub(r"[\s,]+|and", "", text.lower()):
        return None
    return sum(_duration_seconds(match.group("amount"), match.group("unit")) for match in parts)


def _normalize_window(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.strip().lower())
    if normalized in {"weekly", "monthly"}:
        return normalized
    if normalized.startswith("five") or normalized.startswith("5"):
        return "5h"
    if normalized.startswith("seven") or normalized.startswith("7"):
        return "7d"
    return normalized.replace(" ", "")


def _windows_in(line: str) -> list[str]:
    windows: list[str] = []
    for match in USAGE_WINDOW_RE.finditer(line):
        window = _normalize_window(match.group("window"))
        if window not in windows:
            windows.append(window)
    return windows


def _format_reset_at(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M KST")
    except ValueError:
        return value


def _parse_datetime_value(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _structured_request_id(payload: dict | None) -> str | None:
    if not payload:
        return None
    for key in ("request_id", "requestId", "request-id"):
        if payload.get(key):
            return str(payload[key])
    return None


def _usage_details(line: str, *, now: float | None = None) -> dict:
    observed = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(now, timezone.utc)
    payload = _structured_error(line)
    structured_type = _structured_error_type(payload)
    windows = _windows_in(line)
    details = {
        "window": windows[0] if windows else "unknown",
        "windows": windows,
        "reset_at": None,
        "reset_source": None,
        "retry_after_seconds": None,
        "recovery_confidence": "unknown",
        "source": "structured_error" if payload else "stderr",
        "evidence": [],
        "request_id": _structured_request_id(payload),
    }
    if structured_type:
        details["evidence"].append(f"error.type={structured_type}")
    if details["request_id"]:
        details["evidence"].append("request_id")
    details["evidence"].extend(f"window={window}" for window in windows)
    if payload:
        structured_retry = None
        for key in ("retry_after_seconds", "retry_after", "retryAfter"):
            if payload.get(key) is not None:
                try:
                    structured_retry = float(payload[key])
                except (TypeError, ValueError):
                    structured_retry = None
                break
        if structured_retry is not None and structured_retry >= 0:
            details.update({
                "reset_at": (observed + timedelta(seconds=structured_retry)).isoformat(),
                "reset_source": "provider_retry_after",
                "retry_after_seconds": int(structured_retry) if structured_retry.is_integer() else structured_retry,
                "recovery_confidence": "exact",
            })
            details["evidence"].append("retry-after")
        structured_reset = None
        for key in ("reset_at", "resetAt", "resets_at"):
            if payload.get(key):
                structured_reset = _parse_datetime_value(payload[key])
                if structured_reset:
                    break
        if details["reset_at"] is None and structured_reset:
            details.update({
                "reset_at": structured_reset.astimezone(timezone.utc).isoformat(),
                "reset_source": "provider_reset_at",
                "recovery_confidence": "exact",
            })
            details["evidence"].append("reset_at")
    reset_after_match = RESET_AFTER_RE.search(line)
    if reset_after_match and details["reset_at"] is None:
        seconds = _parse_duration(reset_after_match.group("duration"))
        if seconds is not None:
            details.update({
                "reset_at": (observed + timedelta(seconds=seconds)).isoformat(),
                "reset_source": "provider_retry_after" if "retry" in reset_after_match.group(0).lower() else "provider_reset_in",
                "retry_after_seconds": (int(seconds) if seconds.is_integer() else seconds)
                if "retry" in reset_after_match.group(0).lower()
                else None,
                "recovery_confidence": "exact" if "retry" in reset_after_match.group(0).lower() else "estimated",
            })
            details["evidence"].append("retry-after" if "retry" in reset_after_match.group(0).lower() else "reset_in")
    reset_at_match = RESET_AT_RE.search(line) if details["reset_at"] is None else None
    if reset_at_match:
        parsed = _parse_datetime_value(reset_at_match.group("value"))
        if parsed:
            details.update({
                "reset_at": parsed.astimezone(timezone.utc).isoformat(),
                "reset_source": "provider_reset_at",
                "recovery_confidence": "exact",
            })
            details["evidence"].append("reset_at")
    if details["reset_at"] is None and details["window"] != "unknown":
        details["recovery_confidence"] = "estimated"
    details["authority"] = _authority_for(
        details["source"], details["recovery_confidence"], details["reset_source"], details["windows"]
    )
    return details


def _request_id_hash(line: str) -> str | None:
    match = re.search(r"(?:request[_ -]?id|x-request-id)\s*[:=]\s*([A-Za-z0-9._-]+)", line, re.I)
    if not match:
        return None
    return hashlib.sha256(match.group(1).encode()).hexdigest()[:12]


def _candidate_alternatives(role: str) -> str:
    candidates = [name for name in TARGETS if name != role]
    if not candidates:
        return "없음"
    return ", ".join(candidates) + " (가용성 미확인)"


def _usage_event(role: str, code: str, line: str, *, now: float | None = None) -> dict:
    details = _usage_details(line, now=now)
    reset_text = _format_reset_at(details["reset_at"])
    confidence_label = {"exact": "정확", "estimated": "추정", "unknown": "미확인"}[details["recovery_confidence"]]
    window_note = f" 제한 창: {', '.join(details['windows'])}" if details["windows"] else ""
    if reset_text:
        recovery = f"재개 시각({confidence_label}): {reset_text}{window_note}"
    elif details["windows"]:
        recovery = f"회복 창: {', '.join(details['windows'])} (시각 {confidence_label}, 정확한 시각 확인 불가)"
    else:
        recovery = "회복 시각: provider 정보 없음"
    title = {
        "usage_limited": "사용량 제한",
        "session_limited": "native CLI 세션 제한 가능성(계정 전체 사용량 미확인)",
        "rate_limited": "요청 빈도 제한",
        "capacity_limited": "provider 용량 제한",
        "service_overloaded": "provider 과부하",
    }.get(code, "provider 제한")
    header = "[Roda 세션 상태 확인]" if code == "session_limited" else (
        "[Roda 사용량 제한 감지]" if code in {"usage_limited", "rate_limited"} else "[Roda provider 상태 감지]"
    )
    if code == "session_limited":
        recovery = "계정 전체 사용량 제한으로 판정하지 않음; fresh-session probe가 필요합니다."
    request_hash = _request_id_hash(line)
    if not request_hash and details.get("request_id"):
        request_hash = hashlib.sha256(details["request_id"].encode()).hexdigest()[:12]
    return {
        "kind": "provider_usage",
        "provider": role,
        "category": code,
        "role": role,
        "code": code,
        "observed_at": datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).isoformat(),
        "fingerprint": _fingerprint(role, code, line),
        "message": (
            f"{header} {role} provider의 {title} 상태입니다.\n"
            f"유형: {code}\n{recovery}\n"
            f"대체 후보: {_candidate_alternatives(role)}\n자동 Codex 복구: 실행하지 않음"
        ),
        "detail": _safe_detail(line),
        "source": details["source"],
        "authority": details["authority"],
        "evidence": details["evidence"],
        "window": details["window"],
        "windows": details["windows"],
        "reset_at": details["reset_at"],
        "reset_source": details["reset_source"],
        "retry_after_seconds": details["retry_after_seconds"],
        "recovery_confidence": details["recovery_confidence"],
        "confidence": details["recovery_confidence"],
        "request_id_hash": request_hash,
        "auto_repair": "blocked",
    }


def _fingerprint(role: str, code: str, line: str) -> str:
    if code == "auth_error":
        # Authentication failures are one persistent operational incident per
        # provider. The CLI commonly emits both an exit diagnostic and a task
        # failure line for the same OAuth problem; raw-line fingerprints turn
        # those two lines into duplicate Telegram alerts.
        return hashlib.sha256(f"{role}:{code}".encode()).hexdigest()[:16]
    normalized = re.sub(r"\d{2,}", "N", line.strip())
    return hashlib.sha256(f"{role}:{code}:{normalized}".encode()).hexdigest()[:16]


def _coalesce_specific_incidents(state: dict) -> None:
    """Merge duplicate auth diagnostics and retire their generic precursor."""
    incidents = state.setdefault("incidents", {})
    roles = {
        str(item.get("role"))
        for item in incidents.values()
        if isinstance(item, dict)
        and item.get("code") == "auth_error"
        and item.get("status") in {"open", "reopened"}
    }
    for role in roles:
        matches = [
            (key, item)
            for key, item in incidents.items()
            if isinstance(item, dict)
            and str(item.get("role")) == role
            and item.get("code") == "auth_error"
            and item.get("status") in {"open", "reopened"}
        ]
        if not matches:
            continue
        canonical_id = _fingerprint(role, "auth_error", "")
        latest_key, latest = max(
            matches,
            key=lambda pair: float(pair[1].get("last_seen_at", pair[1].get("first_seen_at", 0)) or 0),
        )
        first_seen = min(float(item.get("first_seen_at", 0) or 0) for _, item in matches)
        last_seen = max(float(item.get("last_seen_at", item.get("first_seen_at", 0)) or 0) for _, item in matches)
        canonical = incidents.setdefault(canonical_id, {
            "incident_id": canonical_id,
            "status": "open",
        })
        canonical.update({
            "role": role,
            "code": "auth_error",
            "status": "open",
            "first_seen_at": first_seen,
            "last_seen_at": last_seen,
            "task_id": str(latest.get("task_id") or ""),
            "detail": str(latest.get("detail") or "")[:1000],
        })
        related_alert_keys = {key for key, _ in matches}
        related_alert_keys.update(
            key
            for key, item in incidents.items()
            if isinstance(item, dict)
            and item.get("code") == "auth_error"
            and str(item.get("role")) == role
            and canonical_id in str(item.get("resolution") or "")
        )
        prior_alerts = [
            float(state.get("alerted", {}).get(key, 0) or 0)
            for key in related_alert_keys
            if state.get("alerted", {}).get(key) is not None
        ]
        if prior_alerts:
            state.setdefault("alerted", {})[canonical_id] = max(prior_alerts)
        for key, item in matches:
            if key == canonical_id:
                continue
            item["status"] = "superseded"
            item["resolved_at"] = last_seen
            item["resolution"] = f"coalesced into persistent auth incident {canonical_id}"
        # A generic execution error shortly before the specific OAuth signal
        # is the same unresolved provider problem, not a second root cause.
        for item in incidents.values():
            if (
                isinstance(item, dict)
                and str(item.get("role")) == role
                and item.get("code") == "execution_error"
                and item.get("status") in {"open", "reopened"}
            ):
                generic_seen = float(item.get("last_seen_at", item.get("first_seen_at", 0)) or 0)
                if 0 <= last_seen - generic_seen <= 3600:
                    item["status"] = "superseded"
                    item["resolved_at"] = last_seen
                    item["resolution"] = f"refined to auth incident {canonical_id}"


SENSITIVE_FIELD_RE = re.compile(
    r"^(?:authorization|cookie|x-api-key|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
    r"token|secret|password|credential|private[_ -]?key)$",
    re.I,
)


def _is_sensitive_field(key: object) -> bool:
    camel_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    normalized = re.sub(r"[^a-z0-9]+", "_", camel_case.lower()).strip("_")
    if SENSITIVE_FIELD_RE.match(normalized):
        return True
    return bool(re.search(r"(?:^|_)(?:api_key|access_token|refresh_token|private_key|secret|password|credential)$", normalized))


def _redact_structured(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_field(key) else _redact_structured(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_structured(item) for item in value]
    return value


def _safe_detail(line: str) -> str:
    detail = line.strip()
    structured = _json_object(detail)
    if structured is not None:
        json_start = detail.find("{")
        prefix = detail[:json_start].strip()
        detail = " ".join(
            part for part in (prefix, json.dumps(_redact_structured(structured), ensure_ascii=False, separators=(",", ":"))) if part
        )
    detail = re.sub(r"https?://\S+", "[URL]", detail)
    detail = re.sub(r"(/Users/|/private/)[^ ]+", "[PATH]", detail)
    detail = re.sub(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)[^\s,;\"'}]+", r"\1[REDACTED]", detail)
    detail = re.sub(
        r"(?i)([\"']?(?:x-api-key|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|secret|password|cookie)[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'}]+",
        r"\1[REDACTED]",
        detail,
    )
    detail = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,})\b", "[REDACTED]", detail)
    return detail[-240:]


def _service_running(label: str) -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and "state = running" in result.stdout


def _planned_restart_active(role: str, *, now: float | None = None) -> bool:
    try:
        payload = json.loads(MAINTENANCE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    roles = payload.get("roles", {}) if isinstance(payload, dict) else {}
    if not isinstance(roles, dict):
        return False
    entry = roles.get(role)
    if not isinstance(entry, dict):
        return False
    try:
        return float(entry.get("expires_at", 0)) > (time.time() if now is None else now)
    except (TypeError, ValueError):
        return False


def _repair_approval_granted(event: dict, *, now: float | None = None) -> bool:
    """Return True only for an explicit, unexpired, fingerprinted approval.

    The approval file is intentionally separate from the health state file so
    a provider failure cannot manufacture its own approval by mutating the
    monitor's ordinary event state.  The file contains metadata only; it must
    be created by an operator or a separately reviewed control plane.
    """
    fingerprint = str(event.get("fingerprint", "")).strip()
    if not fingerprint or AUTO_REPAIR_APPROVAL_FILE.is_symlink() or not AUTO_REPAIR_APPROVAL_FILE.is_file():
        return False
    try:
        if AUTO_REPAIR_APPROVAL_FILE.stat().st_mode & 0o077:
            return False
    except OSError:
        return False
    try:
        payload = json.loads(AUTO_REPAIR_APPROVAL_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    approvals = payload.get("approvals", {}) if isinstance(payload, dict) else {}
    approval = approvals.get(fingerprint) if isinstance(approvals, dict) else None
    if not isinstance(approval, dict) or approval.get("approved") is not True:
        return False
    try:
        expires_at = float(approval.get("expires_at", 0))
    except (TypeError, ValueError):
        return False
    return expires_at > (time.time() if now is None else now)


def _send_alert(text: str) -> None:
    if DRY_RUN:
        print(text)
        return
    mode = TOKEN_FILE.stat().st_mode & 0o777
    if mode & 0o077:
        raise RuntimeError(f"Roda token permissions are unsafe: {oct(mode)}")
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("Roda token is empty")
    for chat_id in ALERT_GROUP_IDS:
        body = urllib.parse.urlencode({"chat_id": str(chat_id), "text": text}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:  # public Telegram API
            if response.status != 200:
                raise RuntimeError(f"Roda alert send failed: HTTP {response.status}")


def _repair_preflight_blocker(event: dict) -> str | None:
    """Return a reason when no repair execution can start.

    This check is intentionally side-effect free so the notifier can avoid
    claiming that recovery started when policy or capability already makes
    execution impossible.
    """
    if not CODEX_DIAGNOSIS_ENABLED:
        return "Codex 자동 진단이 비활성화되어 있습니다."
    if not CODEX_BIN.is_file() or not (SOURCE_REPO / ".git").exists():
        return "Codex 진단을 실행할 수 없습니다(provider 또는 기준 저장소 없음)."
    if not AUTO_REPAIR_ENABLED:
        return "자동 복구가 비활성화되어 있습니다."
    if not _repair_approval_granted(event):
        return "자동 복구 승인 없음: fingerprint별 운영자 승인이 필요합니다."
    return None


def _run_codex_repair_impl(event: dict, state: dict) -> str:
    blocker = _repair_preflight_blocker(event)
    if blocker is not None:
        return blocker
    fingerprint = str(event["fingerprint"])
    worktree = REPAIR_ROOT / fingerprint
    if not worktree.exists():
        REPAIR_ROOT.mkdir(parents=True, exist_ok=True)
        lifecycle = repository_lifecycle_lock(SOURCE_REPO) if repository_lifecycle_lock else contextlib.nullcontext()
        with lifecycle:
            if not worktree.exists():
                created = subprocess.run(
                    ["/usr/bin/git", "-C", str(SOURCE_REPO), "worktree", "add", "--detach", str(worktree), "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
        if created.returncode != 0:
            return "Codex 진단 worktree 생성에 실패했습니다."
    prompt = (
        "장애 원인을 파악하고 최소 범위의 안전한 개선을 구현하라. "
        "작업 worktree에서만 수정하고, 토큰·인증·.env·삭제·reset·외부 전송은 절대 수행하지 말라. "
        "변경 후 관련 문법/테스트를 실행하고 원인·변경·검증·롤백을 요약하라.\n\n"
        f"대상 provider: {event['role']}\n"
        f"감지 코드: {event['code']}\n"
        f"관측 세부: {event['detail']}\n"
    )
    try:
        result = subprocess.run(
            [str(CODEX_BIN), "exec", "--json", "-s", "workspace-write", "--skip-git-repo-check", "-C", str(worktree), "--", prompt],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Codex 진단 실행 실패: {type(exc).__name__}"
    if result.returncode != 0:
        return f"Codex 자동 복구 provider 오류(exit={result.returncode})"
    answers: list[str] = []
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("type") == "item.completed":
            payload = item.get("item") or {}
            if payload.get("type") == "agent_message" and payload.get("text"):
                answers.append(str(payload["text"]))
    summary = (answers[-1] if answers else result.stdout.strip())[-3000:]
    status = subprocess.run(["/usr/bin/git", "-C", str(worktree), "status", "--porcelain"], capture_output=True, text=True, check=False)
    changed = [line[3:] for line in status.stdout.splitlines() if len(line) >= 4]
    forbidden = (".env", ".token", "token", "secret", "credential", ".pem", ".key")
    if not changed:
        return f"Codex가 변경을 만들지 않았습니다.\n{summary}"
    if any(line.startswith("D") or any(word in path.lower() for word in forbidden) for line, path in zip(status.stdout.splitlines(), changed)):
        return "Codex 변경이 보호 정책에 걸려 자동 병합하지 않았습니다."
    check = subprocess.run(["/usr/bin/git", "-C", str(worktree), "diff", "--check"], capture_output=True, text=True, check=False)
    if check.returncode != 0:
        return f"Codex 변경의 diff check가 실패해 자동 병합하지 않았습니다: {check.stderr[-500:]}"
    commit = subprocess.run(["/usr/bin/git", "-C", str(worktree), "add", "-A"], capture_output=True, text=True, check=False)
    if commit.returncode != 0:
        return "Codex repair worktree stage에 실패했습니다."
    commit = subprocess.run(["/usr/bin/git", "-C", str(worktree), "commit", "-m", f"fix: automated Telegram health repair {fingerprint}"], capture_output=True, text=True, check=False)
    if commit.returncode != 0:
        return "Codex repair commit에 실패했습니다."
    repair_commit = subprocess.run(["/usr/bin/git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
    if not repair_commit:
        return "Codex repair commit hash를 확인하지 못했습니다."
    result = _merge_repair_commit_and_restart(role=event["role"], code=event["code"], repair_commit=repair_commit, fingerprint=fingerprint)
    if _repair_succeeded(result):
        return f"{result}\n{summary}"
    if _is_retryable_merge_failure(result):
        state.setdefault("pending_merges", {})[fingerprint] = {
            "role": event["role"],
            "code": event["code"],
            "repair_commit": repair_commit,
            "worktree": str(worktree),
            "summary": summary,
            "queued_at": time.time(),
        }
    return result


def _is_retryable_merge_failure(message: str) -> bool:
    return (
        message.startswith("main 작업공간이 깨끗하지 않아")
        or message.startswith("Codex 수정은 생성됐지만 main 병합에 실패했습니다")
    )


def _merge_repair_commit_and_restart(*, role: str, code: str, repair_commit: str, fingerprint: str) -> str:
    integration = integration_lock(SOURCE_REPO) if integration_lock else contextlib.nullcontext()
    try:
        with integration:
            status_result = subprocess.run(
                ["/usr/bin/git", "-C", str(SOURCE_REPO), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=False,
            )
            if status_result.returncode != 0:
                return "main 작업공간 상태를 확인하지 못해 자동 병합하지 않았습니다."
            if status_result.stdout.splitlines():
                return "main 작업공간이 깨끗하지 않아 자동 병합하지 않았습니다."
            head_result = subprocess.run(
                ["/usr/bin/git", "-C", str(SOURCE_REPO), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            if head_result.returncode != 0 or not head_result.stdout.strip():
                return "main HEAD를 확인하지 못해 자동 병합하지 않았습니다."
            source_head = head_result.stdout.strip()
            merge = subprocess.run(["/usr/bin/git", "-C", str(SOURCE_REPO), "merge", "--no-ff", repair_commit, "-m", f"merge: automated Telegram health repair {fingerprint}"], capture_output=True, text=True, check=False)
            if merge.returncode != 0:
                merge_head = subprocess.run(["/usr/bin/git", "-C", str(SOURCE_REPO), "rev-parse", "-q", "--verify", "MERGE_HEAD"], capture_output=True, text=True, check=False).stdout.strip()
                if merge_head:
                    aborted = subprocess.run(["/usr/bin/git", "-C", str(SOURCE_REPO), "merge", "--abort"], capture_output=True, text=True, check=False)
                    if aborted.returncode != 0:
                        return (
                            "CRITICAL: 자동 병합 실패 후 merge --abort도 실패했습니다. "
                            f"main을 수동 점검해야 합니다: {aborted.stderr[-500:]}"
                        )
                post_status = subprocess.run(
                    ["/usr/bin/git", "-C", str(SOURCE_REPO), "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                post_head = subprocess.run(
                    ["/usr/bin/git", "-C", str(SOURCE_REPO), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()
                if post_status.returncode != 0 or post_status.stdout.splitlines() or post_head != source_head:
                    return "CRITICAL: 자동 병합 실패 후 main이 원래 clean HEAD로 복구됐는지 확인하지 못했습니다."
                return f"Codex 수정은 생성됐지만 main 병합에 실패했습니다: {merge.stderr[-500:]}"
    except Exception as exc:
        return f"Codex 수정은 생성됐지만 통합 락을 획득하지 못했습니다: {type(exc).__name__}"
    restart = subprocess.run(
        [
            sys.executable,
            str(RESTART_HELPER),
            role,
            "--reason",
            f"Roda repair for {code}",
        ],
        capture_output=True,
        text=True,
        timeout=360,
        check=False,
    )
    if restart.returncode != 0:
        return f"Codex 수정 병합 완료, 서비스 재기동 실패: {restart.stderr[-500:]}"
    return f"Codex 자동 수정·main 병합·{role} 서비스 재기동 완료."


def _retry_pending_merges(state: dict) -> None:
    """Retry merge for repair commits that were queued because main was
    dirty or a transient merge conflict blocked them (P2 in
    docs/roda-parallel-recovery-improvement-plan.md). Runs every poll cycle
    so a repair that failed to merge once still lands automatically once
    the blocker clears, instead of being a dead end."""
    pending = state.setdefault("pending_merges", {})
    if not pending:
        return
    current = time.time()
    for fingerprint, info in list(pending.items()):
        age = current - float(info.get("queued_at", current))
        if age >= PENDING_MERGE_TTL_SECONDS:
            pending.pop(fingerprint, None)
            expiry_message = (
                f"Codex 수정은 생성됐지만 {PENDING_MERGE_TTL_SECONDS}초 내 병합되지 않아 대기열에서 제외되었습니다. "
                f"수동 병합이 필요합니다.\nworktree={info.get('worktree')}\ncommit={info.get('repair_commit')}"
            )
            state.setdefault("repair_results", {})[fingerprint] = expiry_message
            _save_state(state)
            try:
                _send_alert(f"[Codex 자동복구 대기열 만료]\n대상: {info.get('role')}\n\n{expiry_message}")
            except Exception as exc:
                print(f"Roda pending-merge expiry alert delivery failed: {type(exc).__name__}: {exc}")
            continue
        if not _repair_approval_granted({
            "role": info.get("role"),
            "code": info.get("code"),
            "fingerprint": fingerprint,
        }):
            # A queued commit must not become an implicit approval.  Keep it
            # for explicit review until the fingerprinted approval expires or
            # an operator removes the queue entry.
            continue
        result = _merge_repair_commit_and_restart(
            role=info["role"], code=info["code"], repair_commit=info["repair_commit"], fingerprint=fingerprint,
        )
        if not _repair_succeeded(result):
            continue  # still blocked; stay queued and retry next cycle, no alert spam
        pending.pop(fingerprint, None)
        summary = info.get("summary", "")
        diagnosis = f"{result}\n{summary}".strip()
        state.setdefault("repair_results", {})[fingerprint] = diagnosis
        state.setdefault("recovery_watch", {})[fingerprint] = {
            "role": info["role"],
            "status": "awaiting_reprocess",
            "created_at": current,
            "deadline": current + RECOVERY_TIMEOUT_SECONDS,
            "notified": False,
        }
        _save_state(state)
        try:
            _send_alert(_format_repair_result({"role": info["role"], "code": info["code"]}, diagnosis))
        except Exception as exc:
            print(f"Roda pending-merge success alert delivery failed: {type(exc).__name__}: {exc}")


def _source_repo_tracked_dirty_lines() -> list[str]:
    """Isolated so tests can stub it out — otherwise every poll_once() call
    would shell out to the real SOURCE_REPO's git status, which is whatever
    this repo's working tree happens to be at test time."""
    if not (SOURCE_REPO / ".git").exists():
        return []
    status = subprocess.run(
        ["/usr/bin/git", "-C", str(SOURCE_REPO), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    return [line for line in status if line and not line.startswith("??")]


def _check_main_dirty(state: dict, current: float) -> dict | None:
    """Surface a dirty main as a first-class health signal (P1) — while it
    persists, every automated repair merge above will fail silently into the
    retry queue, so a human needs to know to commit/stash it."""
    tracked_dirty = _source_repo_tracked_dirty_lines()
    if not tracked_dirty:
        state["main_dirty_alerted_at"] = 0
        return None
    last_alerted = float(state.get("main_dirty_alerted_at", 0) or 0)
    if current - last_alerted < MAIN_DIRTY_ALERT_INTERVAL_SECONDS:
        return None
    state["main_dirty_alerted_at"] = current
    return {
        "role": "system",
        "code": "main_dirty",
        "fingerprint": f"main-dirty:{int(current // MAIN_DIRTY_ALERT_INTERVAL_SECONDS)}",
        "message": (
            "[Roda 감지] main 저장소에 커밋되지 않은 추적 변경이 있습니다. "
            "이 상태가 남아있는 한 모든 Codex 자동복구 병합이 실패합니다."
        ),
        "detail": "\n".join(tracked_dirty[:20]),
    }


def _run_codex_repair(event: dict, state: dict) -> str:
    """Run repair and persist one provider-neutral Roda→Codex handoff."""
    fingerprint = str(event["fingerprint"])
    session_id = f"sess-roda-{fingerprint}"
    session_error = ""
    try:
        from edge_agent_session_bridge import start_session, update_session

        session_id = start_session(
            task_id=f"roda-{fingerprint}",
            channel="internal",
            provider="codex",
            owner="roda-health",
            workspace=str(SOURCE_REPO),
            worktree=str(REPAIR_ROOT / fingerprint),
        )
    except (ImportError, OSError, ValueError) as exc:
        # Health detection must remain operational on the system Python used
        # by legacy tests; session persistence is an additive handoff layer.
        session_error = f"세션 기록 생략: {type(exc).__name__}"

    diagnosis = _run_codex_repair_impl(event, state)
    succeeded = _repair_succeeded(diagnosis)
    try:
        from edge_agent_session_bridge import update_session

        update_session(
            session_id,
            status="succeeded" if succeeded else "failed",
            summary=diagnosis[-8000:],
            next_action=("문제 봇 재처리 결과 확인" if succeeded else "Codex 진단·수정 실패 원인 확인"),
            workspace=str(SOURCE_REPO),
            worktree=str(REPAIR_ROOT / fingerprint),
            verification={"repair_succeeded": succeeded, "target_role": event["role"], "detection_code": event["code"]},
            event_type="roda_repair_completed",
        )
    except (ImportError, FileNotFoundError, OSError, ValueError) as exc:
        session_error = f"세션 갱신 생략: {type(exc).__name__}"
    suffix = f"\n논리 세션: {session_id}"
    if session_error:
        suffix += f" ({session_error})"
    return diagnosis + suffix


def _repair_succeeded(diagnosis: str) -> bool:
    return "Codex 자동 수정·main 병합" in diagnosis and "서비스 재기동 완료" in diagnosis


def _format_repair_result(event: dict, diagnosis: str) -> str:
    succeeded = _repair_succeeded(diagnosis)
    not_started = any(marker in diagnosis for marker in (
        "비활성화되어 있습니다",
        "승인 없음",
        "실행할 수 없습니다",
    ))
    status = "성공" if succeeded else ("미실행" if not_started else "미완료/실패")
    message = (
        f"[Codex 자동복구 결과]\n"
        f"대상: {event['role']}\n"
        f"상태: {status}\n"
        f"감지 코드: {event['code']}\n\n"
        f"{diagnosis}"
    )
    if succeeded:
        message += (
            f"\n\n@{BOT_USERNAMES.get(event['role'], event['role'])} "
            "Codex가 수정한 내용을 확인하고 이전 문제를 다시 처리하세요."
        )
    elif not_started:
        message += "\n\n자동 수정은 시작되지 않았으며 문제 봇에 재처리를 지시하지 않습니다."
    else:
        message += "\n\n자동 수정이 완료되지 않았으므로 문제 봇에 재처리를 지시하지 않습니다."
    return message


def _usage_watch_expiry(event: dict, created_at: float) -> float:
    reset_at = event.get("reset_at")
    if reset_at:
        try:
            reset_timestamp = datetime.fromisoformat(str(reset_at).replace("Z", "+00:00")).timestamp()
            return max(created_at + USAGE_WATCH_TTL_SECONDS, reset_timestamp + USAGE_WATCH_GRACE_SECONDS)
        except (TypeError, ValueError, OverflowError):
            pass
    return created_at + USAGE_WATCH_TTL_SECONDS


def _expire_usage_watches(state: dict, current: float) -> None:
    for usage in state.get("usage_watch", {}).values():
        if usage.get("status") in {"waiting_for_probe", "probe_started"}:
            try:
                expired = current >= float(usage.get("expires_at", float(usage.get("created_at", current)) + USAGE_WATCH_TTL_SECONDS))
            except (TypeError, ValueError):
                expired = True
            if expired:
                usage["status"] = "expired"
                usage["expired_at"] = current


def _active_usage_watch(state: dict, role: str) -> tuple[str, dict] | None:
    candidates = [
        (fingerprint, usage)
        for fingerprint, usage in state.get("usage_watch", {}).items()
        if usage.get("role") == role and usage.get("status") in {"waiting_for_probe", "probe_started"}
    ]
    if not candidates:
        return None
    def created_at(item: tuple[str, dict]) -> float:
        try:
            return float(item[1].get("created_at", 0))
        except (TypeError, ValueError):
            return 0.0

    return max(candidates, key=created_at)


def _upsert_usage_watch(state: dict, event: dict, current: float) -> bool:
    role = event["role"]
    active = _active_usage_watch(state, role)
    if active:
        _, usage = active
        usage["last_seen_at"] = current
        usage["source"] = event.get("source", usage.get("source", "stderr"))
        usage["authority"] = event.get("authority", usage.get("authority", "local_log"))
        usage["evidence"] = list(event.get("evidence", usage.get("evidence", [])))
        if event.get("reset_at"):
            usage["reset_at"] = event["reset_at"]
            usage["reset_source"] = event.get("reset_source")
            usage["recovery_confidence"] = event.get("recovery_confidence")
            try:
                created_at = float(usage.get("created_at", current))
            except (TypeError, ValueError):
                created_at = current
            usage["expires_at"] = _usage_watch_expiry(event, created_at)
        for window in event.get("windows", []):
            if window not in usage.setdefault("windows", []):
                usage["windows"].append(window)
        return False
    fingerprint = event["fingerprint"]
    state["usage_watch"][fingerprint] = {
        "role": role,
        "status": "waiting_for_probe",
        "created_at": current,
        "last_seen_at": current,
        "reset_at": event.get("reset_at"),
        "reset_source": event.get("reset_source"),
        "recovery_confidence": event.get("recovery_confidence"),
        "source": event.get("source", "stderr"),
        "authority": event.get("authority", "local_log"),
        "evidence": list(event.get("evidence", [])),
        "window": event.get("window", "unknown"),
        "windows": list(event.get("windows", [])),
        "expires_at": _usage_watch_expiry(event, current),
        "notified": False,
    }
    return True


def _prune_alerted(state: dict, current: float) -> None:
    for fingerprint, timestamp in list(state.get("alerted", {}).items()):
        try:
            expired = current - float(timestamp) >= ALERT_RETENTION_SECONDS
        except (TypeError, ValueError):
            expired = True
        if expired:
            state["alerted"].pop(fingerprint, None)


def _record_metric(state: dict, code: str, current: float) -> None:
    metrics = state.setdefault("metrics", {})
    classified = metrics.setdefault("classified", {})
    try:
        previous = int(classified.get(code, 0))
    except (TypeError, ValueError):
        previous = 0
    classified[code] = previous + 1
    metrics["last_event_at"] = datetime.fromtimestamp(current, timezone.utc).isoformat()


def _upsert_incident(state: dict, event: dict, current: float) -> None:
    if event.get("kind") in {"recovery_result", "usage_recovery"}:
        return
    fingerprint = str(event.get("fingerprint") or "")
    if not fingerprint:
        return
    incidents = state.setdefault("incidents", {})
    incident = incidents.setdefault(fingerprint, {
        "incident_id": fingerprint,
        "first_seen_at": current,
        "status": "open",
    })
    incident.update({
        "role": str(event.get("role") or "unknown"),
        "code": str(event.get("code") or "unknown"),
        "task_id": str(event.get("task_id") or incident.get("task_id") or ""),
        "detail": _safe_detail(str(event.get("detail") or ""))[:1000],
        "last_seen_at": current,
    })
    if incident.get("status") == "resolved":
        incident["status"] = "reopened"


def _resolve_task_incidents(state: dict, role: str, task_id: str, current: float) -> None:
    if not task_id:
        return
    for incident in state.setdefault("incidents", {}).values():
        if incident.get("role") == role and incident.get("task_id") == task_id and incident.get("status") in {"open", "reopened"}:
            incident["status"] = "resolved"
            incident["resolved_at"] = current
            incident["resolution"] = "matching terminal task event observed"


def _record_diagnostic_observation(state: dict, line: str, role: str, code: str | None) -> None:
    if not line or re.search(r"\btext\s*=", line, re.I) or not UNKNOWN_SIGNAL_RE.search(line):
        return
    metrics = state.setdefault("metrics", {})
    metrics["diagnostic_lines"] = int(metrics.get("diagnostic_lines", 0) or 0) + 1
    payload = _structured_error(line)
    if payload is not None and _structured_category(payload, role) is None:
        metrics["parse_failures"] = int(metrics.get("parse_failures", 0) or 0) + 1
    if code is None:
        metrics["unknown"] = int(metrics.get("unknown", 0) or 0) + 1


def _route_incident(role: str, code: str) -> str:
    """Fixed owner table (design doc 확정 사항 1, rule 1).

    ``main_dirty`` belongs to the repo's existing auto-repair authority
    (Codex — see ``_run_codex_repair_impl``); every other code belongs to
    the role the failure was already observed on.
    """
    if code == "main_dirty":
        return "codex"
    return role


def _check_repeat_failure(state: dict, role: str, code: str, current: float) -> bool:
    """Same (role, code) already reached full deliberation within the
    repeat-failure window (design doc 확정 사항 3)."""
    for record in state.get("deliberation_history", []):
        if record.get("role") != role or record.get("code") != code:
            continue
        try:
            triggered_at = float(record.get("triggered_at", 0))
        except (TypeError, ValueError):
            continue
        if current - triggered_at <= REPEAT_FAILURE_WINDOW_SECONDS:
            return True
    return False


def _find_mergeable_incident(state: dict, role: str, current: float) -> str | None:
    """An open, already-routed, non-attached incident for the same role
    seen within the merge window (design doc 확정 사항 2)."""
    candidates = []
    for fingerprint, incident in state.get("incidents", {}).items():
        if incident.get("role") != role:
            continue
        if incident.get("status") not in {"open", "reopened"}:
            continue
        if incident.get("escalation_stage") in (None, "attached"):
            continue
        try:
            last_seen = float(incident.get("last_seen_at", 0))
        except (TypeError, ValueError):
            continue
        if current - last_seen <= INCIDENT_MERGE_WINDOW_SECONDS:
            candidates.append((last_seen, fingerprint))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _route_incident_event(state: dict, event: dict, current: float) -> None:
    if event.get("kind") in {"recovery_result", "usage_recovery", "escalation_notice"}:
        _upsert_incident(state, event, current)
        return
    fingerprint = str(event.get("fingerprint") or "")
    is_new = fingerprint not in state.get("incidents", {})
    _upsert_incident(state, event, current)
    incident = state["incidents"].get(fingerprint)
    if incident is None or not is_new:
        return
    role = str(event.get("role") or "unknown")
    code = str(event.get("code") or "unknown")
    if _check_repeat_failure(state, role, code, current):
        incident["escalation_stage"] = "human_notified"
        incident["escalation_reason"] = "repeat_failure"
        incident["escalated_at"] = current
        event["message"] += (
            f"\n\n⚠️ 같은 문제({role}/{code})가 {REPEAT_FAILURE_WINDOW_SECONDS // 86400}일 내 "
            "전체 디벨리버레이션까지 갔다가 반복되었습니다. 에이전트 체인을 건너뛰고 사람 확인이 필요합니다."
        )
        return
    merge_target = _find_mergeable_incident(state, role, current)
    if merge_target is not None:
        incident["escalation_stage"] = "attached"
        incident["attached_to"] = merge_target
        primary = state["incidents"][merge_target]
        primary.setdefault("related_incidents", [])
        if fingerprint not in primary["related_incidents"]:
            primary["related_incidents"].append(fingerprint)
        return
    routed_role = _route_incident(role, code)
    incident["escalation_stage"] = "awaiting_ack"
    incident["routed_role"] = routed_role
    incident["routed_at"] = current
    incident["ack_deadline"] = current + ROUTING_ACK_TIMEOUT_SECONDS
    incident["reroute_count"] = 0
    incident["related_incidents"] = []
    event["message"] += f"\n\n@{BOT_USERNAMES.get(routed_role, routed_role)} 확인 요망 — 이 인시던트의 담당자입니다."


def _record_incident_ack(state: dict, role: str, pending_id: str, current: float) -> None:
    """Any new pending task for the routed role counts as an ack.

    The exact task_id the routed bot will use to answer an incident mention
    cannot be predicted in advance (it is assigned by that bot's own bridge
    when it starts handling the message), so ack detection is deliberately
    coarse: activity from the routed role after ``routed_at`` and before
    ``ack_deadline`` is treated as acknowledgement. Completion (Task 4) is
    then tracked precisely against this specific ``pending_id``.
    """
    for incident in state.get("incidents", {}).values():
        if incident.get("routed_role") != role or incident.get("escalation_stage") != "awaiting_ack":
            continue
        incident["escalation_stage"] = "acked"
        incident["ack_task_id"] = pending_id
        incident["acked_at"] = current
        incident["completion_deadline"] = current + ROUTING_COMPLETION_TIMEOUT_SECONDS


def _dispatch_antigravity_escalation(state: dict, fingerprint: str, incident: dict, reason: str, current: float) -> dict:
    incident["escalation_stage"] = "pending_antigravity_triage"
    incident["escalation_reason"] = reason
    incident["escalated_at"] = current
    reason_label = {"no_ack": "5분 내 담당자 응답 없음", "no_completion": "24시간 내 처리 완료 확인 없음"}.get(reason, reason)
    return {
        "kind": "escalation_notice",
        "role": incident.get("role", "unknown"),
        "code": "escalation",
        "fingerprint": f"escalation:{fingerprint}:{reason}:{int(current)}",
        "message": (
            f"[Roda 에스컬레이션] incident={fingerprint} (role={incident.get('role')}, code={incident.get('code')})\n"
            f"사유: {reason_label}\n세부: {incident.get('detail', '')}\n\n"
            f"@{BOT_USERNAMES.get('antigravity', 'antigravity')} 판단 요청 — "
            "담당자 다운/매핑 오류/판단 어려움 중 하나로 감별해 주세요."
        ),
        "detail": f"incident={fingerprint}; reason={reason}",
    }


def _check_ack_timeouts(state: dict, current: float) -> list[dict]:
    events = []
    for fingerprint, incident in state.get("incidents", {}).items():
        if incident.get("escalation_stage") != "awaiting_ack":
            continue
        try:
            deadline = float(incident.get("ack_deadline", 0))
        except (TypeError, ValueError):
            continue
        if current >= deadline:
            events.append(_dispatch_antigravity_escalation(state, fingerprint, incident, "no_ack", current))
    return events


def _resolve_incident_ack_completion(state: dict, role: str, task_id: str, current: float) -> None:
    if not task_id:
        return
    for incident in state.get("incidents", {}).values():
        if (
            incident.get("routed_role") == role
            and incident.get("escalation_stage") == "acked"
            and incident.get("ack_task_id") == task_id
        ):
            incident["escalation_stage"] = "completed"
            incident["completed_at"] = current


def _check_completion_timeouts(state: dict, current: float) -> list[dict]:
    events = []
    for fingerprint, incident in state.get("incidents", {}).items():
        if incident.get("escalation_stage") != "acked":
            continue
        try:
            deadline = float(incident.get("completion_deadline", 0))
        except (TypeError, ValueError):
            continue
        if current >= deadline:
            events.append(_dispatch_antigravity_escalation(state, fingerprint, incident, "no_completion", current))
    return events


_TRIAGE_VERDICT_RE = re.compile(r"VERDICT:\s*(OWNER_DOWN|MISROUTED|JUDGMENT_HARD)", re.I)
_TRIAGE_OWNER_RE = re.compile(r"OWNER:\s*(claude|codex|antigravity)", re.I)
_TRIAGE_VALID_OWNERS = frozenset({"claude", "codex", "antigravity"})


def _build_triage_prompt(incident: dict) -> str:
    return (
        "다음 인시던트를 감별하라. 정확히 아래 세 가지 중 하나로만 판정하고, "
        "반드시 첫 줄에 `VERDICT: <값>` 형식으로 답하라 (값은 OWNER_DOWN, MISROUTED, "
        "JUDGMENT_HARD 중 하나). MISROUTED인 경우 두 번째 줄에 반드시 "
        "`OWNER: <claude|codex|antigravity>` 형식으로 실제 담당자를 지정하라.\n\n"
        "- OWNER_DOWN: 라우팅된 담당자 자체가 다운/에러 상태라 응답할 수 없는 경우\n"
        "- MISROUTED: 매핑이 틀렸고 실제 책임자가 다른 경우 (OWNER 지정 필수)\n"
        "- JUDGMENT_HARD: 담당자는 멀쩡하지만 원인 판단이 어려운 경우\n\n"
        f"인시던트 role: {incident.get('role')}\n"
        f"감지 코드: {incident.get('code')}\n"
        f"라우팅된 담당자: {incident.get('routed_role')}\n"
        f"에스컬레이션 사유: {incident.get('escalation_reason')}\n"
        f"재라우팅 횟수: {incident.get('reroute_count', 0)}\n"
        f"세부: {incident.get('detail', '')}\n"
    )


def _parse_triage_verdict(text: str) -> dict:
    match = _TRIAGE_VERDICT_RE.search(text or "")
    if not match:
        return {"verdict": "JUDGMENT_HARD", "owner": None}
    verdict = match.group(1).upper()
    if verdict != "MISROUTED":
        return {"verdict": verdict, "owner": None}
    owner_match = _TRIAGE_OWNER_RE.search(text or "")
    if not owner_match:
        return {"verdict": "JUDGMENT_HARD", "owner": None}
    owner = owner_match.group(1).lower()
    if owner not in _TRIAGE_VALID_OWNERS:
        return {"verdict": "JUDGMENT_HARD", "owner": None}
    return {"verdict": "MISROUTED", "owner": owner}


def poll_once(state: dict, *, now: float | None = None) -> list[dict]:
    current = now if now is not None else time.time()
    alerts: list[dict] = []
    _migrate_state(state)
    state.setdefault("offsets", {})
    state.setdefault("pending", {})
    state.setdefault("alerted", {})
    state.setdefault("delivery_retry", [])
    state.setdefault("repair_results", {})
    state.setdefault("recovery_watch", {})
    state.setdefault("usage_watch", {})
    state.setdefault("service_down_since", {})
    state.setdefault("pending_merges", {})
    state.setdefault("main_dirty_alerted_at", 0)
    _prune_alerted(state, current)
    _expire_usage_watches(state, current)
    alerts.extend(_check_ack_timeouts(state, current))
    alerts.extend(_check_completion_timeouts(state, current))
    retry_events = list(state["delivery_retry"])
    state["delivery_retry"] = []
    alerts.extend(event for event in retry_events if event.get("code") not in IGNORED_RETRY_CODES)
    main_dirty_event = _check_main_dirty(state, current)
    if main_dirty_event:
        alerts.append(main_dirty_event)
    for role, target in TARGETS.items():
        path = Path(target["log"])
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        previous = int(state["offsets"].get(role, 0))
        if not state.get("initialized"):
            state["offsets"][role] = size
            continue
        if size < previous:
            previous = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(previous)
                lines = handle.readlines()
                state["offsets"][role] = handle.tell()
        except FileNotFoundError:
            lines = []
        for line_index, raw in enumerate(lines):
            line = raw.strip()
            task_id = _line_task_id(line)
            role_pending = state["pending"].setdefault(role, {})
            if START_RE.search(line):
                pending_id = task_id or f"legacy-{role}-{int(current)}-{line_index}"
                role_pending[pending_id] = current
                _record_incident_ack(state, role, pending_id, current)
            if DONE_RE.search(line):
                if task_id:
                    role_pending.pop(task_id, None)
                    _resolve_task_incidents(state, role, task_id, current)
                    _resolve_incident_ack_completion(state, role, task_id, current)
                else:
                    # Compatibility for logs written before task correlation:
                    # an unkeyed terminal line may close only an unkeyed start.
                    # It must never consume a modern task or a different ID.
                    legacy_ids = [item for item in role_pending if item.startswith("legacy-")]
                    if legacy_ids:
                        oldest = min(legacy_ids, key=lambda item: float(role_pending[item]))
                        role_pending.pop(oldest, None)
            code = classify_line(line, role=role)
            _record_diagnostic_observation(state, line, role, code)
            if code:
                _record_metric(state, code, current)
                event = _usage_event(role, code, line, now=current) if code in RESOURCE_RECOVERY_CODES else {
                    "provider": role,
                    "category": code,
                    "role": role,
                    "code": code,
                    "observed_at": datetime.fromtimestamp(current, timezone.utc).isoformat(),
                    "fingerprint": _fingerprint(role, code, line),
                    "source": "stderr",
                    "authority": "local_log",
                    "confidence": "unknown",
                    "evidence": [],
                    "message": f"[Roda 감지] {role} 봇에 {code} 문제가 발생했습니다. 확인이 필요합니다.",
                    "detail": _safe_detail(line),
                }
                fingerprint = event["fingerprint"]
                if code in RESOURCE_RECOVERY_CODES:
                    created = _upsert_usage_watch(state, event, current)
                    if created and fingerprint in state["alerted"]:
                        state["alerted"].pop(fingerprint, None)
                    if created and fingerprint not in state["alerted"]:
                        alerts.append(event)
                        state["alerted"][fingerprint] = current
                elif fingerprint not in state["alerted"]:
                    alerts.append(event)
                    state["alerted"][fingerprint] = current
            for recovery in state["recovery_watch"].values():
                if recovery.get("role") != role or recovery.get("status") in {"completed_success", "completed_failure", "timeout"}:
                    continue
                if START_RE.search(line):
                    recovery["status"] = "reprocess_started"
                    recovery["started_at"] = current
                elif recovery.get("status") == "reprocess_started" and DONE_RE.search(line):
                    recovery["status"] = "completed_failure" if "처리 실패" in line else "completed_success"
            active_usage = _active_usage_watch(state, role)
            if active_usage:
                fingerprint, usage = active_usage
                if usage.get("status") == "waiting_for_probe" and START_RE.search(line):
                    usage["status"] = "probe_started"
                    usage["started_at"] = current
                elif usage.get("status") == "probe_started" and DONE_RE.search(line):
                    if re.search(r"처리 완료", line):
                        usage["status"] = "completed_success"
                        usage["completed_at"] = current
                        if not usage.get("notified"):
                            alerts.append({
                                "kind": "usage_recovery",
                                "role": role,
                                "code": "usage_recovered",
                                "fingerprint": f"usage-recovery:{fingerprint}",
                                "message": f"[Roda 사용량 회복 확인] {role} provider가 제한 이후 요청을 정상 완료했습니다.",
                                "detail": f"원래 감지={fingerprint}",
                            })
                            usage["notified"] = True
                    else:
                        usage["status"] = "probe_failed"
                        usage["completed_at"] = current
        if _planned_restart_active(role, now=current):
            state["service_down_since"].pop(role, None)
        elif not _service_running(target["label"]):
            first_seen = state["service_down_since"].setdefault(role, current)
            if current - float(first_seen) >= SERVICE_DOWN_GRACE_SECONDS:
                fingerprint = _fingerprint(role, "service_down", target["label"])
                if fingerprint not in state["alerted"]:
                    alerts.append({"role": role, "code": "service_down", "fingerprint": fingerprint, "message": f"[Roda 감지] {role} Telegram 봇 프로세스가 {SERVICE_DOWN_GRACE_SECONDS}초 이상 실행되지 않았습니다. 확인이 필요합니다.", "detail": "launchd service is not running after grace period"})
                    state["alerted"][fingerprint] = current
        else:
            state["service_down_since"].pop(role, None)
        pending = state["pending"].get(role, {})
        for pending_id, started_at in sorted(pending.items(), key=lambda item: float(item[1])):
            if current - float(started_at) < NO_RESPONSE_SECONDS:
                continue
            fingerprint = _fingerprint(role, "no_response", pending_id)
            if fingerprint not in state["alerted"]:
                alerts.append({"role": role, "code": "no_response", "fingerprint": fingerprint, "task_id": pending_id, "message": f"[Roda 감지] {role} 봇이 요청을 시작했지만 {NO_RESPONSE_SECONDS}초 내 완료·오류 기록이 없습니다. 확인이 필요합니다.", "detail": f"task={pending_id}; no completion/error event for {NO_RESPONSE_SECONDS}s"})
                state["alerted"][fingerprint] = current
    for recovery_fingerprint, recovery in state["recovery_watch"].items():
        if recovery.get("status") in {"awaiting_reprocess", "reprocess_started"} and current >= float(recovery["deadline"]):
            recovery["status"] = "timeout"
        if recovery.get("status") in {"completed_success", "completed_failure", "timeout"} and not recovery.get("notified"):
            status = recovery["status"]
            label = {"completed_success": "재처리 성공", "completed_failure": "재처리 실패", "timeout": "재처리 확인 시간 초과"}[status]
            alerts.append({
                "kind": "recovery_result",
                "role": recovery["role"],
                "code": status,
                "fingerprint": f"recovery:{recovery_fingerprint}:{status}",
                "message": f"[Roda 복구 확인] {recovery['role']} 봇 {label}",
                "detail": f"recovery fingerprint={recovery_fingerprint}",
                "recovery_fingerprint": recovery_fingerprint,
            })
            recovery["notified"] = True
            if status == "completed_success":
                incident = state.setdefault("incidents", {}).get(recovery_fingerprint)
                if incident:
                    incident["status"] = "resolved"
                    incident["resolved_at"] = current
                    incident["resolution"] = "post-repair request completed successfully"
    for event in alerts:
        _route_incident_event(state, event, current)
    state["initialized"] = True
    return alerts


def _process_cycle(state: dict) -> None:
    _retry_pending_merges(state)
    alerts = poll_once(state)
    _save_state(state)
    for event in alerts:
        try:
            if event.get("kind") in {"recovery_result", "usage_recovery"}:
                _send_alert(f"{event['message']}\n세부: {event['detail']}")
                continue
            fingerprint = str(event["fingerprint"])
            if event.get("code") in NON_REPAIRABLE_CODES or event.get("auto_repair") == "blocked":
                # A depleted provider cannot be repaired by changing local
                # code. Alert once and wait for a real subsequent request to
                # prove recovery; never spend Codex usage on diagnosis.
                _send_alert(f"{event['message']}\n세부: {event['detail']}")
                blocked_reason = {
                    "main_dirty": "main 저장소 추적 변경 — 사람 확인 필요",
                    "usage_limited": "사용량 제한 이벤트 — 자동복구 차단",
                    "session_limited": "native CLI 세션 제한 가능성 — 계정 전체 사용량으로 단정하지 않고 fresh-session 확인 대기",
                    "rate_limited": "요청 빈도 제한 이벤트 — 자동복구 차단",
                    "capacity_limited": "provider 용량 제한 이벤트 — 자동복구 차단",
                    "service_overloaded": "provider 과부하 이벤트 — 자동복구 차단",
                    "context_exceeded": "컨텍스트 제한 이벤트 — 자동복구 차단",
                    "auth_error": "인증 오류 이벤트 — 자동복구 차단",
                }.get(event.get("code"), "자동복구 차단 이벤트")
                state["repair_results"][fingerprint] = blocked_reason
                _save_state(state)
                continue
            diagnosis = state["repair_results"].get(fingerprint)
            if diagnosis is None:
                blocker = _repair_preflight_blocker(event)
                if blocker is None:
                    try:
                        _send_alert(
                            f"{event['message']}\n세부: {event['detail']}\n\n"
                            "[Codex 자동복구 시작]\n원인 분석과 안전 검증을 진행합니다."
                        )
                    except Exception as exc:
                        print(f"Roda detection alert delivery failed: {type(exc).__name__}: {exc}")
            if diagnosis is None:
                diagnosis = _run_codex_repair(event, state)
                state["repair_results"][fingerprint] = diagnosis
                if _repair_succeeded(diagnosis):
                    state["recovery_watch"][fingerprint] = {
                        "role": event["role"],
                        "status": "awaiting_reprocess",
                        "created_at": time.time(),
                        "deadline": time.time() + RECOVERY_TIMEOUT_SECONDS,
                        "notified": False,
                    }
                _save_state(state)
            _send_alert(_format_repair_result(event, diagnosis))
        except Exception as exc:
            state.setdefault("delivery_retry", []).append(event)
            _save_state(state)
            print(f"Roda health alert delivery failed: {type(exc).__name__}: {exc}")


def main() -> int:
    _harden_log_permissions()
    state = _load_state()
    if "--once" in sys.argv[1:]:
        try:
            _process_cycle(state)
        except Exception as exc:
            print(f"Roda health monitor cycle failed: {type(exc).__name__}: {exc}")
            return 1
        return 0
    while True:
        try:
            _process_cycle(state)
        except Exception as exc:
            print(f"Roda health monitor cycle failed: {type(exc).__name__}: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
