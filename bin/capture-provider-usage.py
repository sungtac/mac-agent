#!/usr/bin/env python3
"""Capture a sanitized Claude/Codex usage snapshot without calling a provider."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def read_usage(coach_bin: str = "coach") -> dict:
    try:
        result = subprocess.run(
            [coach_bin, "--json", "--providers", "claude,codex"],
            check=False,
            capture_output=True,
            text=True,
            # Claude OAuth may briefly fail while Keychain is refreshing and
            # coach then uses its non-web CLI fallback. Two providers plus
            # that fallback can legitimately exceed the old 15s bound.
            # This is a background snapshot, not an interactive gate.
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"usage query failed: {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("usage query returned no usable data")
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("usage query returned invalid JSON") from exc
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        raise RuntimeError("usage query did not contain providers")

    sanitized = {}
    for provider in ("claude", "codex"):
        source = providers.get(provider)
        if not isinstance(source, dict):
            continue
        windows = source.get("windows")
        if not isinstance(windows, dict):
            continue
        clean_windows = {}
        for name, window in windows.items():
            if not isinstance(name, str) or not isinstance(window, dict):
                continue
            left_pct = window.get("left_pct")
            if isinstance(left_pct, (int, float)) and 0 <= left_pct <= 100:
                clean = {"left_pct": left_pct}
                for reset_key in ("reset_at", "resetAt", "resets_at"):
                    if isinstance(window.get(reset_key), str):
                        clean["reset_at"] = window[reset_key]
                        break
                clean_windows[name] = clean
        if clean_windows:
            sanitized[provider] = {"windows": clean_windows}
    if not sanitized:
        raise RuntimeError("usage query contained no usable provider windows")
    return {
        "schema": "edge_agent_provider_usage_snapshot.v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "providers": sanitized,
    }


def append_snapshot(snapshot: dict, output: Path) -> None:
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{output}.lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as temp:
                if output.exists():
                    temp.write(output.read_text(encoding="utf-8"))
                temp.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n")
                temp.flush()
                os.fsync(temp.fileno())
            os.replace(temp_name, output)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(Path.home() / ".claude" / "provider-usage-snapshots.jsonl"))
    parser.add_argument("--coach-bin", default="coach")
    args = parser.parse_args()
    try:
        snapshot = read_usage(args.coach_bin)
        append_snapshot(snapshot, Path(args.output))
    except RuntimeError as exc:
        print(f"status=unavailable\nreason={exc}", file=sys.stderr)
        return 75
    print(f"status=recorded\noutput={Path(args.output).expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
