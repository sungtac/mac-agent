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
TOKEN_FILE = Path(os.environ.get("RODA_GEMMA_TOKEN_FILE", "~/.config/roda-gemma/telegram.token")).expanduser()
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
DRY_RUN = os.environ.get("RODA_GEMMA_HEALTH_DRY_RUN", "0") == "1"
CODEX_DIAGNOSIS_ENABLED = os.environ.get("RODA_GEMMA_CODEX_DIAGNOSIS_ENABLED", "1") == "1"
AUTO_REPAIR_ENABLED = os.environ.get("RODA_GEMMA_AUTO_REPAIR_ENABLED", "1") == "1"
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
        "label": "com.macagent.telegram-codex",
        "log": HOME / ".claude/hooks-state/telegram-codex/stderr.log",
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
EXPLICIT_USAGE_LIMIT_RE = re.compile(
    r"hit your (?:session|usage) limit|(?:usage|session) limit\s*(?:reached|exceeded|exhausted)|usage cap|"
    r"you have reached your (?:session|usage) limit|quota\s*(?:exceeded|reached|exhausted)|"
    r"(?:limit|quota)\s*(?:exceeded|reached|exhausted)|resource_exhausted|"
    r"baseline quota.*(?:reached|exhausted)|사용량\s*(?:제한|한도)|쿼터\s*(?:초과|소진)",
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
FAILURE_CONTEXT_RE = re.compile(r"error|fail|실패|오류|exit\s*=|status code|HTTP", re.I)
EXPLICIT_RATE_LIMIT_RE = re.compile(
    r"rate[_ -]?limit(?:_error)|rate[_ -]?limit(?:s)?\s+(?:exceeded|reached)|"
    r"too many requests|HTTP\s*429|status code\s*:?\s*429|\b429\b",
    re.I,
)
NON_REPAIRABLE_CODES = frozenset({"usage_limited", "rate_limited", "context_exceeded", "auth_error"})
USAGE_RECOVERY_CODES = frozenset({"usage_limited", "rate_limited"})
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


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)
    os.chmod(path, 0o600)


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "initialized": False,
            "offsets": {},
            "pending": {},
            "alerted": {},
            "delivery_retry": [],
            "repair_results": {},
            "recovery_watch": {},
            "usage_watch": {},
        }


def _save_state(state: dict) -> None:
    _atomic_write(STATE_FILE, state)


def classify_line(line: str) -> str | None:
    prompt_marker = re.search(r"\btext\s*=", line, re.I)
    provider_prefix = line[:prompt_marker.start()] if prompt_marker else line
    if prompt_marker and not FAILURE_CONTEXT_RE.search(provider_prefix):
        usage_candidate = None
        rate_candidate = None
    else:
        usage_candidate = USAGE_LIMIT_RE.search(line)
        rate_candidate = RATE_LIMIT_RE.search(line)
    if CONTEXT_LIMIT_RE.search(line) and not (prompt_marker and not FAILURE_CONTEXT_RE.search(provider_prefix)):
        return "context_exceeded"
    if rate_candidate and (EXPLICIT_RATE_LIMIT_RE.search(line) or FAILURE_CONTEXT_RE.search(provider_prefix)):
        return "rate_limited"
    if usage_candidate and (EXPLICIT_USAGE_LIMIT_RE.search(line) or FAILURE_CONTEXT_RE.search(provider_prefix)):
        return "usage_limited"
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


def _usage_details(line: str, *, now: float | None = None) -> dict:
    observed = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(now, timezone.utc)
    windows = _windows_in(line)
    details = {
        "window": windows[0] if windows else "unknown",
        "windows": windows,
        "reset_at": None,
        "reset_source": None,
        "retry_after_seconds": None,
        "recovery_confidence": "unknown",
    }
    reset_after_match = RESET_AFTER_RE.search(line)
    if reset_after_match:
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
    reset_at_match = RESET_AT_RE.search(line) if details["reset_at"] is None else None
    if reset_at_match:
        try:
            parsed = datetime.fromisoformat(reset_at_match.group("value").replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            details.update({
                "reset_at": parsed.astimezone(timezone.utc).isoformat(),
                "reset_source": "provider_reset_at",
                "recovery_confidence": "exact",
            })
        except ValueError:
            pass
    if details["reset_at"] is None and details["window"] != "unknown":
        details["recovery_confidence"] = "estimated"
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
    if reset_text:
        recovery = f"재개 예상: {reset_text}"
    elif details["window"] != "unknown":
        recovery = f"회복 창: {details['window']} (정확한 시각 확인 불가)"
    else:
        recovery = "회복 시각: provider 정보 없음"
    source = "structured_error" if re.search(r"(?:error\.)?type\s*[:=]|request[_ -]?id\s*[:=]", line, re.I) else "stderr"
    return {
        "kind": "provider_usage",
        "provider": role,
        "category": code,
        "role": role,
        "code": code,
        "observed_at": datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).isoformat(),
        "fingerprint": _fingerprint(role, code, line),
        "message": (
            f"[Roda 사용량 제한 감지] {role} provider의 사용량 제한 상태입니다.\n"
            f"유형: {code}\n{recovery}\n"
            f"대체 후보: {_candidate_alternatives(role)}\n자동 Codex 복구: 실행하지 않음"
        ),
        "detail": _safe_detail(line),
        "source": source,
        "window": details["window"],
        "windows": details["windows"],
        "reset_at": details["reset_at"],
        "reset_source": details["reset_source"],
        "retry_after_seconds": details["retry_after_seconds"],
        "recovery_confidence": details["recovery_confidence"],
        "confidence": details["recovery_confidence"],
        "request_id_hash": _request_id_hash(line),
        "auto_repair": "blocked",
    }


def _fingerprint(role: str, code: str, line: str) -> str:
    normalized = re.sub(r"\d{2,}", "N", line.strip())
    return hashlib.sha256(f"{role}:{code}:{normalized}".encode()).hexdigest()[:16]


def _safe_detail(line: str) -> str:
    detail = line.strip()
    detail = re.sub(r"https?://\S+", "[URL]", detail)
    detail = re.sub(r"(/Users/|/private/)[^ ]+", "[PATH]", detail)
    detail = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1[REDACTED]", detail)
    detail = re.sub(
        r"(?i)((?:x-api-key|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|secret|password|cookie)\s*[:=]\s*)[^\s,;]+",
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


def _run_codex_repair_impl(event: dict) -> str:
    if not CODEX_DIAGNOSIS_ENABLED:
        return "Codex 자동 진단이 비활성화되어 있습니다."
    if not CODEX_BIN.is_file() or not (SOURCE_REPO / ".git").exists():
        return "Codex 진단을 실행할 수 없습니다(provider 또는 기준 저장소 없음)."
    fingerprint = str(event["fingerprint"])
    if not AUTO_REPAIR_ENABLED:
        return "자동 복구가 비활성화되어 있습니다."
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
    integration = integration_lock(SOURCE_REPO) if integration_lock else contextlib.nullcontext()
    try:
        with integration:
            source_status = subprocess.run(["/usr/bin/git", "-C", str(SOURCE_REPO), "status", "--porcelain"], capture_output=True, text=True, check=False).stdout.splitlines()
            if any(line and not line.startswith("??") for line in source_status):
                return "main에 추적 파일 변경이 있어 자동 병합하지 않았습니다."
            source_head = subprocess.run(["/usr/bin/git", "-C", str(SOURCE_REPO), "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
            merge = subprocess.run(["/usr/bin/git", "-C", str(SOURCE_REPO), "merge", "--no-ff", repair_commit, "-m", f"merge: automated Telegram health repair {fingerprint}"], capture_output=True, text=True, check=False)
            if merge.returncode != 0:
                current_head = subprocess.run(["/usr/bin/git", "-C", str(SOURCE_REPO), "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
                merge_head = subprocess.run(["/usr/bin/git", "-C", str(SOURCE_REPO), "rev-parse", "-q", "--verify", "MERGE_HEAD"], capture_output=True, text=True, check=False).stdout.strip()
                if current_head == source_head and merge_head == repair_commit:
                    subprocess.run(["/usr/bin/git", "-C", str(SOURCE_REPO), "merge", "--abort"], capture_output=True, text=True, check=False)
                return f"Codex 수정은 생성됐지만 main 병합에 실패했습니다: {merge.stderr[-500:]}"
    except Exception as exc:
        return f"Codex 수정은 생성됐지만 통합 락을 획득하지 못했습니다: {type(exc).__name__}"
    restart = subprocess.run(
        [
            sys.executable,
            str(RESTART_HELPER),
            event["role"],
            "--reason",
            f"Roda repair for {event['code']}",
        ],
        capture_output=True,
        text=True,
        timeout=360,
        check=False,
    )
    if restart.returncode != 0:
        return f"Codex 수정 병합 완료, 서비스 재기동 실패: {restart.stderr[-500:]}"
    return f"Codex 자동 수정·main 병합·{event['role']} 서비스 재기동 완료.\n{summary}"


def _run_codex_repair(event: dict) -> str:
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

    diagnosis = _run_codex_repair_impl(event)
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
    status = "성공" if succeeded else "미완료/실패"
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
        "window": event.get("window", "unknown"),
        "windows": list(event.get("windows", [])),
        "expires_at": _usage_watch_expiry(event, current),
        "notified": False,
    }
    return True


def poll_once(state: dict, *, now: float | None = None) -> list[dict]:
    current = now if now is not None else time.time()
    alerts: list[dict] = []
    state.setdefault("offsets", {})
    state.setdefault("pending", {})
    state.setdefault("alerted", {})
    state.setdefault("delivery_retry", [])
    state.setdefault("repair_results", {})
    state.setdefault("recovery_watch", {})
    state.setdefault("usage_watch", {})
    state.setdefault("service_down_since", {})
    _expire_usage_watches(state, current)
    retry_events = list(state["delivery_retry"])
    state["delivery_retry"] = []
    alerts.extend(event for event in retry_events if event.get("code") not in IGNORED_RETRY_CODES)
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
        for raw in lines:
            line = raw.strip()
            if START_RE.search(line):
                state["pending"].setdefault(role, []).append(current)
            if DONE_RE.search(line) and state["pending"].get(role):
                state["pending"][role].pop(0)
            code = classify_line(line)
            if code:
                event = _usage_event(role, code, line, now=current) if code in USAGE_RECOVERY_CODES else {
                    "role": role,
                    "code": code,
                    "fingerprint": _fingerprint(role, code, line),
                    "message": f"[Roda 감지] {role} 봇에 {code} 문제가 발생했습니다. 확인이 필요합니다.",
                    "detail": _safe_detail(line),
                }
                fingerprint = event["fingerprint"]
                if code in USAGE_RECOVERY_CODES:
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
        pending = state["pending"].get(role, [])
        if pending and current - pending[0] >= NO_RESPONSE_SECONDS:
            fingerprint = _fingerprint(role, "no_response", str(int(pending[0])))
            if fingerprint not in state["alerted"]:
                alerts.append({"role": role, "code": "no_response", "fingerprint": fingerprint, "message": f"[Roda 감지] {role} 봇이 요청을 시작했지만 {NO_RESPONSE_SECONDS}초 내 완료·오류 기록이 없습니다. 확인이 필요합니다.", "detail": f"no completion/error event for {NO_RESPONSE_SECONDS}s"})
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
    state["initialized"] = True
    return alerts


def _process_cycle(state: dict) -> None:
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
                state["repair_results"][fingerprint] = "사용량 제한 이벤트 — 자동복구 차단"
                _save_state(state)
                continue
            diagnosis = state["repair_results"].get(fingerprint)
            if diagnosis is None:
                try:
                    _send_alert(
                        f"{event['message']}\n세부: {event['detail']}\n\n"
                        "[Codex 자동복구 시작]\n원인 분석과 안전 검증을 진행합니다."
                    )
                except Exception as exc:
                    print(f"Roda detection alert delivery failed: {type(exc).__name__}: {exc}")
            if diagnosis is None:
                diagnosis = _run_codex_repair(event)
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
