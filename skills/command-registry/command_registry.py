#!/usr/bin/env python3
"""Minimal command registry CLI.

This registry validates command strings only. It never executes commands.

Commands:
- check <command>
- update_success <command>
- update_fail <failed_command> <reason> <replacement_command>

Default store: state/command_registry_records.json
Override for tests: COMMAND_REGISTRY_STORE=/tmp/store.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_STORE = Path(
    os.environ.get("EDGE_AGENT_STATE_ROOT", "~/.edge-agent/state")
).expanduser().resolve() / "skills" / "command_registry_records.json"
LOCK_TIMEOUT_SECONDS = 5.0


def store_path() -> Path:
    raw = os.environ.get("COMMAND_REGISTRY_STORE")
    return Path(raw).expanduser().resolve() if raw else DEFAULT_STORE


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def normalize_command(command: str) -> str:
    return " ".join(command.strip().split())


def default_store() -> dict[str, Any]:
    return {"verified": [], "blacklisted": []}


def load_store(path: Path | None = None) -> dict[str, Any]:
    path = path or store_path()
    if not path.exists():
        return default_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid command registry JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid command registry shape: expected object: {path}")
    verified = data.get("verified", [])
    blacklisted = data.get("blacklisted", [])
    if not isinstance(verified, list) or not isinstance(blacklisted, list):
        raise ValueError(f"invalid command registry shape: verified/blacklisted must be lists: {path}")
    return {"verified": verified, "blacklisted": blacklisted}


def write_store(data: dict[str, Any], path: Path | None = None) -> None:
    path = path or store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()


def _blacklisted_entry(data: dict[str, Any], command: str) -> dict[str, Any] | None:
    norm = normalize_command(command)
    for entry in data.get("blacklisted", []):
        if isinstance(entry, dict) and normalize_command(str(entry.get("command", ""))) == norm:
            return entry
    return None


def _is_verified(data: dict[str, Any], command: str) -> bool:
    norm = normalize_command(command)
    for entry in data.get("verified", []):
        if isinstance(entry, str) and normalize_command(entry) == norm:
            return True
        if isinstance(entry, dict) and normalize_command(str(entry.get("command", ""))) == norm:
            return True
    return False


def check_command(command: str, *, path: Path | None = None) -> str:
    data = load_store(path)
    blacklisted = _blacklisted_entry(data, command)
    if blacklisted is not None:
        replacement = str(blacklisted.get("replacement", "")).strip()
        return f"BLACKLISTED -> 대체: {replacement}" if replacement else "BLACKLISTED -> 대체: <none>"
    if _is_verified(data, command):
        return "VALID"
    return "UNKNOWN"


def update_success(command: str, *, path: Path | None = None) -> None:
    path = path or store_path()
    command = normalize_command(command)
    if not command:
        raise ValueError("command must not be empty")
    with file_lock(path):
        data = load_store(path)
        if not _is_verified(data, command):
            data["verified"].append({"command": command, "updated_at": int(time.time())})
        write_store_without_lock(data, path)


def update_fail(command: str, reason: str, replacement: str, *, path: Path | None = None) -> None:
    path = path or store_path()
    command = normalize_command(command)
    replacement = normalize_command(replacement)
    reason = reason.strip()
    if not command:
        raise ValueError("failed command must not be empty")
    if not replacement:
        raise ValueError("replacement command must not be empty")
    with file_lock(path):
        data = load_store(path)
        existing = _blacklisted_entry(data, command)
        entry = {"command": command, "reason": reason, "replacement": replacement, "updated_at": int(time.time())}
        if existing is None:
            data["blacklisted"].append(entry)
        else:
            existing.update(entry)
        write_store_without_lock(data, path)


def write_store_without_lock(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate command strings against a local registry; never executes commands")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Check a command string")
    p_check.add_argument("command_string")

    p_success = sub.add_parser("update_success", help="Record a command string as verified")
    p_success.add_argument("command_string")

    p_fail = sub.add_parser("update_fail", help="Blacklist a failed command string and store a replacement")
    p_fail.add_argument("failed_command")
    p_fail.add_argument("reason")
    p_fail.add_argument("replacement_command")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            print(check_command(args.command_string))
            return 0
        if args.command == "update_success":
            update_success(args.command_string)
            print("RECORDED_SUCCESS")
            return 0
        if args.command == "update_fail":
            update_fail(args.failed_command, args.reason, args.replacement_command)
            print("RECORDED_FAIL")
            return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
