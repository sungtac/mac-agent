#!/usr/bin/env python3
"""Detect Telegram provider failures and send deduplicated Roda alerts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


HOME = Path.home()
STATE_FILE = Path(os.environ.get("RODA_GEMMA_HEALTH_STATE_FILE", "~/.edge-agent/state/telegram-health-monitor.json")).expanduser()
TOKEN_FILE = Path(os.environ.get("RODA_GEMMA_TOKEN_FILE", "~/.config/roda-gemma/telegram.token")).expanduser()
POLL_SECONDS = int(os.environ.get("RODA_GEMMA_HEALTH_POLL_SECONDS", "30"))
NO_RESPONSE_SECONDS = int(os.environ.get("RODA_GEMMA_HEALTH_NO_RESPONSE_SECONDS", "300"))
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

ERROR_PATTERNS = (
    ("empty_response", re.compile(r"빈 응답|empty response", re.I)),
    ("execution_error", re.compile(r"실행 오류|실행에 실패|처리 실패|exit=\d+", re.I)),
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
DONE_RE = re.compile(r"처리 완료|처리 실패")


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
        return {"initialized": False, "offsets": {}, "pending": {}, "alerted": {}, "delivery_retry": [], "repair_results": {}}


def _save_state(state: dict) -> None:
    _atomic_write(STATE_FILE, state)


def classify_line(line: str) -> str | None:
    for code, pattern in ERROR_PATTERNS:
        if pattern.search(line):
            return code
    return None


def _fingerprint(role: str, code: str, line: str) -> str:
    normalized = re.sub(r"\d{2,}", "N", line.strip())
    return hashlib.sha256(f"{role}:{code}:{normalized}".encode()).hexdigest()[:16]


def _safe_detail(line: str) -> str:
    detail = re.sub(r"https?://\S+", "[URL]", line.strip())
    detail = re.sub(r"(/Users/|/private/)[^ ]+", "[PATH]", detail)
    return detail[-240:]


def _service_running(label: str) -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and "state = running" in result.stdout


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


def _run_codex_repair(event: dict) -> str:
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
    source_status = subprocess.run(["/usr/bin/git", "-C", str(SOURCE_REPO), "status", "--porcelain"], capture_output=True, text=True, check=False).stdout.splitlines()
    if any(line and not line.startswith("??") for line in source_status):
        return "main에 추적 파일 변경이 있어 자동 병합하지 않았습니다."
    merge = subprocess.run(["/usr/bin/git", "-C", str(SOURCE_REPO), "merge", "--no-ff", repair_commit, "-m", f"merge: automated Telegram health repair {fingerprint}"], capture_output=True, text=True, check=False)
    if merge.returncode != 0:
        subprocess.run(["/usr/bin/git", "-C", str(SOURCE_REPO), "merge", "--abort"], capture_output=True, text=True, check=False)
        return f"Codex 수정은 생성됐지만 main 병합에 실패했습니다: {merge.stderr[-500:]}"
    restart = subprocess.run(["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{TARGETS[event['role']]['label']}"], capture_output=True, text=True, check=False)
    if restart.returncode != 0:
        return f"Codex 수정 병합 완료, 서비스 재기동 실패: {restart.stderr[-500:]}"
    return f"Codex 자동 수정·main 병합·{event['role']} 서비스 재기동 완료.\n{summary}"


def poll_once(state: dict, *, now: float | None = None) -> list[dict]:
    current = now if now is not None else time.time()
    alerts: list[dict] = []
    state.setdefault("offsets", {})
    state.setdefault("pending", {})
    state.setdefault("alerted", {})
    state.setdefault("delivery_retry", [])
    state.setdefault("repair_results", {})
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
                fingerprint = _fingerprint(role, code, line)
                if fingerprint not in state["alerted"]:
                    alerts.append({"role": role, "code": code, "fingerprint": fingerprint, "message": f"[Roda 감지] {role} 봇에 {code} 문제가 발생했습니다. 확인이 필요합니다.", "detail": _safe_detail(line)})
                    state["alerted"][fingerprint] = current
        if not _service_running(target["label"]):
            fingerprint = _fingerprint(role, "service_down", target["label"])
            if fingerprint not in state["alerted"]:
                alerts.append({"role": role, "code": "service_down", "fingerprint": fingerprint, "message": f"[Roda 감지] {role} Telegram 봇 프로세스가 실행 중이 아닙니다. 확인이 필요합니다.", "detail": "launchd service is not running"})
                state["alerted"][fingerprint] = current
        pending = state["pending"].get(role, [])
        if pending and current - pending[0] >= NO_RESPONSE_SECONDS:
            fingerprint = _fingerprint(role, "no_response", str(int(pending[0])))
            if fingerprint not in state["alerted"]:
                alerts.append({"role": role, "code": "no_response", "fingerprint": fingerprint, "message": f"[Roda 감지] {role} 봇이 요청을 시작했지만 {NO_RESPONSE_SECONDS}초 내 완료·오류 기록이 없습니다. 확인이 필요합니다.", "detail": f"no completion/error event for {NO_RESPONSE_SECONDS}s"})
                state["alerted"][fingerprint] = current
    state["initialized"] = True
    return alerts


def _process_cycle(state: dict) -> None:
    alerts = poll_once(state)
    _save_state(state)
    for event in alerts:
        try:
            fingerprint = str(event["fingerprint"])
            diagnosis = state["repair_results"].get(fingerprint)
            if diagnosis is None:
                diagnosis = _run_codex_repair(event)
                state["repair_results"][fingerprint] = diagnosis
                _save_state(state)
            alert = (
                f"{event['message']}\n세부: {event['detail']}\n\n"
                f"[Codex 자동 원인 분석·개선]\n{diagnosis}\n\n"
                f"@{BOT_USERNAMES.get(event['role'], event['role'])} Codex가 수정한 내용을 확인하고 이전 문제를 다시 처리하세요."
            )
            _send_alert(alert)
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
