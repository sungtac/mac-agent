#!/usr/bin/env python3
"""Run the repository-owned skill tests without relying on pytest discovery."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "bin"))
from edge_agent_skill_catalog import load_catalog  # noqa: E402


def discover_tests() -> tuple[str, ...]:
    discovered: set[str] = set()
    for entry in load_catalog()["skills"]:
        for relative in entry.get("tests", []):
            repo_candidate = ROOT / relative
            candidate = repo_candidate if repo_candidate.exists() else ROOT / "skills" / relative
            if candidate.is_file():
                paths = [candidate] if candidate.name.startswith("test") and candidate.suffix == ".py" else []
            elif candidate.is_dir():
                paths = sorted(path for path in candidate.rglob("test*.py") if path.is_file())
            else:
                paths = []
            discovered.update(str(path.relative_to(ROOT)) for path in paths)
    return tuple(sorted(discovered))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run all repository-owned skill tests")
    parser.add_argument("--quiet", action="store_true", help="suppress successful test output")
    args = parser.parse_args(argv)
    tests = discover_tests()
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), environment.get("PYTHONPATH", "")]))
    failures: list[str] = []
    for relative in tests:
        result = subprocess.run([sys.executable, str(ROOT / relative)], cwd=ROOT, env=environment, text=True, capture_output=True)
        if result.returncode:
            failures.append(relative)
            print(f"FAIL {relative}", file=sys.stderr)
            if result.stdout:
                print(result.stdout, file=sys.stderr, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
        elif not args.quiet:
            print(f"PASS {relative}")
    if failures:
        print(f"{len(failures)} skill test file(s) failed", file=sys.stderr)
        return 1
    print(f"PASS: {len(tests)} skill test files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
