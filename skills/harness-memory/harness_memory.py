#!/usr/bin/env python3
"""Minimal durable harness memory CLI.

Commands:
- search/query <keywords>
- add_success <date> <situation> <steps_json_array> <result>
- add_fail <date> <situation> <attempted_steps_json_array> <failure_reason>

Default store: state/harness_memory_records.json
Override for tests: HARNESS_MEMORY_STORE=/tmp/store.json
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
import ipaddress
import re
from urllib.parse import urlsplit

DEFAULT_STORE = Path(
    os.environ.get("EDGE_AGENT_STATE_ROOT", "~/.edge-agent/state")
).expanduser().resolve() / "skills" / "harness_memory_records.json"
LOCK_TIMEOUT_SECONDS = 5.0
SENSITIVE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]+\b"),
    re.compile(r"\b(?:ghp|github_pat)[_-][A-Za-z0-9_-]+\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9_-]+\b"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:token|secret|password|api[_ -]?key|authorization)\s*[:=]\s*[^\s,'\"]+",
        re.IGNORECASE,
    ),
)
URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
SENSITIVE_KEYS = {
    "token", "secret", "password", "api_key", "authorization", "cookie",
    "private_key", "client_secret", "access_token", "refresh_token", "credential",
}


def store_path() -> Path:
    raw = os.environ.get("HARNESS_MEMORY_STORE")
    return Path(raw).expanduser().resolve() if raw else DEFAULT_STORE


def is_sensitive_key(key: Any) -> bool:
    return str(key).strip().casefold().replace("-", "_").replace(" ", "_") in SENSITIVE_KEYS


def is_private_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if not hostname:
            return False
        if parsed.username or parsed.password:
            return True
        if hostname.lower() in {"localhost", "localhost.localdomain"} or hostname.lower().endswith(".local"):
            return True
        try:
            return ipaddress.ip_address(hostname).is_private or ipaddress.ip_address(hostname).is_loopback or ipaddress.ip_address(hostname).is_link_local or ipaddress.ip_address(hostname).is_reserved
        except ValueError:
            return False
    except ValueError:
        return False


def contains_sensitive(value: Any) -> bool:
    if isinstance(value, str):
        if any(pattern.search(value) for pattern in SENSITIVE_PATTERNS):
            return True
        return any(is_private_url(match.group(0)) for match in URL_PATTERN.finditer(value))
    if isinstance(value, dict):
        return any(is_sensitive_key(key) or contains_sensitive(key) or contains_sensitive(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_sensitive(item) for item in value)
    return False


def redact_text(value: str) -> str:
    def redact_url(match: re.Match[str]) -> str:
        return "[REDACTED_URL]" if is_private_url(match.group(0)) else match.group(0)

    redacted = URL_PATTERN.sub(redact_url, value)
    for pattern in SENSITIVE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: "[REDACTED]" if is_sensitive_key(key) else redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    fd: int | None = None
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


def load_records(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or store_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid harness memory store JSON: {path}: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"invalid harness memory store shape: expected list: {path}")
    records: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            records.append(redact_value(item))
    return records


def write_records(records: list[dict[str, Any]], path: Path | None = None) -> None:
    if contains_sensitive(records):
        raise ValueError("harness memory refuses sensitive values")
    path = path or store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()


def parse_steps(raw: str) -> list[Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"steps must be a JSON array: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("steps must be a JSON array")
    return data


def searchable_text(record: dict[str, Any]) -> str:
    parts = [
        record.get("type"),
        record.get("date"),
        record.get("situation"),
        record.get("result"),
        record.get("failure_reason"),
        json.dumps(record.get("steps", []), ensure_ascii=False),
    ]
    return "\n".join(str(part) for part in parts if part is not None).lower()


def search(query: str, *, path: Path | None = None) -> list[dict[str, Any]]:
    query_norm = query.strip().lower()
    if not query_norm:
        return []
    tokens = [token for token in query_norm.split() if token]
    records = load_records(path)
    matches: list[dict[str, Any]] = []
    for record in records:
        haystack = searchable_text(record)
        if all(token in haystack for token in tokens):
            matches.append(record)
    return matches


def append_record(record: dict[str, Any], *, path: Path | None = None) -> None:
    if contains_sensitive(record):
        raise ValueError("harness memory refuses sensitive values")
    path = path or store_path()
    with file_lock(path):
        records = load_records(path)
        records.append(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()


def print_search_result(matches: list[dict[str, Any]]) -> int:
    if not matches:
        print("NO_MATCH")
        return 0
    print(json.dumps({"matches": matches, "count": len(matches)}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search and record harness troubleshooting memory")
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="Search previous troubleshooting records")
    p_search.add_argument("query")

    p_query = sub.add_parser("query", help="Alias of search")
    p_query.add_argument("query")

    p_success = sub.add_parser("add_success", help="Record a successful troubleshooting procedure")
    p_success.add_argument("date")
    p_success.add_argument("situation")
    p_success.add_argument("steps_json_array")
    p_success.add_argument("result")

    p_fail = sub.add_parser("add_fail", help="Record a failed troubleshooting attempt")
    p_fail.add_argument("date")
    p_fail.add_argument("situation")
    p_fail.add_argument("attempted_steps_json_array")
    p_fail.add_argument("failure_reason")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"search", "query"}:
            return print_search_result(search(args.query))
        if args.command == "add_success":
            append_record(
                {
                    "type": "success",
                    "date": args.date,
                    "situation": args.situation,
                    "steps": parse_steps(args.steps_json_array),
                    "result": args.result,
                }
            )
            print("RECORDED_SUCCESS")
            return 0
        if args.command == "add_fail":
            append_record(
                {
                    "type": "fail",
                    "date": args.date,
                    "situation": args.situation,
                    "steps": parse_steps(args.attempted_steps_json_array),
                    "failure_reason": args.failure_reason,
                }
            )
            print("RECORDED_FAIL")
            return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
