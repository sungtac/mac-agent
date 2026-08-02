#!/usr/bin/env python3
"""Drain-aware restart helper for the Telegram agent LaunchAgents.

The helper refuses to kill a provider while its latest request is active. It
publishes a short-lived maintenance marker so the Roda health monitor does not
turn an intentional restart into an incident or start an unnecessary repair.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


HOME = Path.home()
MAINTENANCE_FILE = Path(
    os.environ.get(
        "EDGE_AGENT_TELEGRAM_MAINTENANCE_FILE",
        str(HOME / ".edge-agent" / "state" / "telegram-maintenance.json"),
    )
).expanduser()
DEFAULT_DRAIN_SECONDS = int(os.environ.get("EDGE_AGENT_TELEGRAM_DRAIN_SECONDS", "300"))
STARTUP_SECONDS = int(os.environ.get("EDGE_AGENT_TELEGRAM_STARTUP_SECONDS", "60"))
MARKER_SECONDS = int(os.environ.get("EDGE_AGENT_TELEGRAM_MARKER_SECONDS", "180"))

TARGETS = {
    "claude": {
        "label": "com.macagent.telegram-claude",
        "log": HOME / ".claude/hooks-state/telegram-claude/stderr.log",
    },
    "codex": {
        "label": "com.multiagent.engine",
        "log": HOME / ".edge-agent/state/multiagent-engine/stderr.log",
        "startup_marker": "Starting canonical Telegram engine",
    },
    "antigravity": {
        "label": "com.macagent.telegram-antigravity",
        "log": HOME / ".claude/hooks-state/telegram-antigravity/stderr.log",
    },
    "roda": {
        "label": "com.macagent.telegram-roda-gemma",
        "log": HOME / ".claude/hooks-state/telegram-roda-gemma/stderr.log",
        "startup_marker": "connected as @",
        "request_start_marker": "request started",
        "request_done_markers": ("request completed", "request failed"),
    },
}


def _read_log(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]
    except FileNotFoundError:
        return []


def request_is_active(
    path: Path,
    startup_marker: str = "Starting direct Telegram",
    request_start_marker: str = "처리 시작",
    request_done_markers: tuple[str, ...] = ("처리 완료", "처리 실패"),
) -> bool:
    """Return true only for a request started after the latest bot startup."""

    latest_start = -1
    latest_done = -1
    latest_boot = -1
    for index, line in enumerate(_read_log(path)):
        if startup_marker in line:
            latest_boot = index
        if request_start_marker in line:
            latest_start = index
        if any(marker in line for marker in request_done_markers):
            latest_done = index
    # An old unmatched start before a later bot startup is stale and must not
    # block every future maintenance operation forever.
    return latest_start > latest_done and latest_start > latest_boot


def service_running(label: str, *, runner=subprocess.run) -> bool:
    result = runner(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and "state = running" in result.stdout


def _load_marker() -> dict[str, object]:
    try:
        payload = json.loads(MAINTENANCE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1, "roles": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("roles"), dict):
        return {"version": 1, "roles": {}}
    return payload


def _write_marker(payload: dict[str, object]) -> None:
    MAINTENANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{MAINTENANCE_FILE.name}.", suffix=".tmp", dir=MAINTENANCE_FILE.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, MAINTENANCE_FILE)
        os.chmod(MAINTENANCE_FILE, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _marker_lock():
    """Serialize concurrent role updates to the shared maintenance marker."""
    lock_path = MAINTENANCE_FILE.with_suffix(MAINTENANCE_FILE.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def set_maintenance(role: str, *, reason: str, expires_at: float) -> None:
    with _marker_lock():
        payload = _load_marker()
        roles = payload.setdefault("roles", {})
        assert isinstance(roles, dict)
        roles[role] = {"reason": reason[:240], "expires_at": expires_at}
        _write_marker(payload)


def clear_maintenance(role: str) -> None:
    with _marker_lock():
        if not MAINTENANCE_FILE.exists():
            return
        payload = _load_marker()
        roles = payload.get("roles", {})
        if not isinstance(roles, dict):
            return
        roles.pop(role, None)
        if roles:
            _write_marker(payload)
        else:
            try:
                MAINTENANCE_FILE.unlink()
            except FileNotFoundError:
                pass


def restart(role: str, *, reason: str = "operator requested restart", drain_seconds: int = DEFAULT_DRAIN_SECONDS, runner=subprocess.run, sleep=time.sleep) -> None:
    target = TARGETS[role]
    deadline = time.monotonic() + max(0, drain_seconds)
    while request_is_active(
        target["log"],
        target.get("startup_marker", "Starting direct Telegram"),
        target.get("request_start_marker", "처리 시작"),
        target.get("request_done_markers", ("처리 완료", "처리 실패")),
    ):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{role} active request did not drain before restart")
        sleep(1)

    before_lines = len(_read_log(target["log"]))
    marker_expires = time.time() + max(MARKER_SECONDS, STARTUP_SECONDS + 30)
    set_maintenance(role, reason=reason, expires_at=marker_expires)
    try:
        result = runner(
            ["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{target['label']}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "launchctl kickstart failed").strip()
            raise RuntimeError(detail[-500:])
        startup_deadline = time.monotonic() + max(1, STARTUP_SECONDS)
        while time.monotonic() < startup_deadline:
            lines = _read_log(target["log"])
            startup_marker = target.get("startup_marker", "Starting direct Telegram")
            booted = any(startup_marker in line for line in lines[before_lines:])
            if booted and service_running(target["label"], runner=runner):
                return
            sleep(1)
        raise TimeoutError(f"{role} did not report healthy startup within {STARTUP_SECONDS}s")
    finally:
        clear_maintenance(role)


def stop(role: str, *, reason: str = "operator requested stop", drain_seconds: int = DEFAULT_DRAIN_SECONDS, runner=subprocess.run, sleep=time.sleep) -> None:
    """Drain active work and stop one Telegram LaunchAgent safely."""
    target = TARGETS[role]
    deadline = time.monotonic() + max(0, drain_seconds)
    while request_is_active(
        target["log"],
        target.get("startup_marker", "Starting direct Telegram"),
        target.get("request_start_marker", "처리 시작"),
        target.get("request_done_markers", ("처리 완료", "처리 실패")),
    ):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{role} active request did not drain before stop")
        sleep(1)

    if not service_running(target["label"], runner=runner):
        return

    marker_expires = time.time() + max(MARKER_SECONDS, 30)
    set_maintenance(role, reason=reason, expires_at=marker_expires)
    try:
        result = runner(
            ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{target['label']}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "launchctl bootout failed").strip()
            raise RuntimeError(detail[-500:])
        if service_running(target["label"], runner=runner):
            raise RuntimeError(f"{role} service remained loaded after stop")
    finally:
        clear_maintenance(role)


def main() -> int:
    parser = argparse.ArgumentParser(description="Drain-aware Telegram agent restart")
    parser.add_argument("role", choices=sorted(TARGETS))
    parser.add_argument("--reason", default="operator requested restart")
    parser.add_argument("--drain-seconds", type=int, default=DEFAULT_DRAIN_SECONDS)
    parser.add_argument("--stop", action="store_true", help="drain and stop instead of restart")
    args = parser.parse_args()
    if args.stop:
        stop(args.role, reason=args.reason, drain_seconds=args.drain_seconds)
        print(f"stopped {args.role}")
    else:
        restart(args.role, reason=args.reason, drain_seconds=args.drain_seconds)
        print(f"restarted {args.role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
