#!/usr/bin/env python3
"""Serialize the repository's full active-test entrypoints.

The test suite intentionally exercises process-wide state and launchd-style
files.  Running two full suites against the same checkout can therefore make
otherwise deterministic routing tests race.  This small wrapper provides a
cross-process lock without creating files inside the working tree.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required after --")

    args.lock.parent.mkdir(parents=True, exist_ok=True)
    with args.lock.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        completed = subprocess.run(command, check=False)
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
