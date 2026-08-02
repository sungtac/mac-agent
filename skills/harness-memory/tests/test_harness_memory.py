#!/usr/bin/env python3
"""Local tests for skills/harness-memory/harness_memory.py."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "harness_memory.py"


def load_module():
    spec = importlib.util.spec_from_file_location("harness_memory_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def run_cli(store: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HARNESS_MEMORY_STORE"] = str(store)
    return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=env)


def test_search_and_query_alias() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "memory.json"
        proc = run_cli(store, "search", "gateway restart")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "NO_MATCH"

        proc = run_cli(store, "add_success", "2026-06-18", "gateway restart stuck", '["check status", "run postcheck"]', "recovered")
        assert proc.returncode == 0, proc.stderr
        assert "RECORDED_SUCCESS" in proc.stdout

        proc = run_cli(store, "search", "gateway recovered")
        assert proc.returncode == 0, proc.stderr
        assert '"count": 1' in proc.stdout
        assert "gateway restart stuck" in proc.stdout

        proc = run_cli(store, "query", "gateway recovered")
        assert proc.returncode == 0, proc.stderr
        assert '"count": 1' in proc.stdout


def test_add_fail_and_invalid_steps_are_safe() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "memory.json"
        proc = run_cli(store, "add_fail", "2026-06-18", "bad command", '["run invalid"]', "command missing")
        assert proc.returncode == 0, proc.stderr
        assert "RECORDED_FAIL" in proc.stdout

        before = store.read_text(encoding="utf-8")
        proc = run_cli(store, "add_success", "2026-06-18", "bad json", '{not-json}', "should not write")
        assert proc.returncode != 0
        assert "steps must be a JSON array" in proc.stderr
        after = store.read_text(encoding="utf-8")
        assert before == after


def test_module_parse_steps_requires_array() -> None:
    module = load_module()
    assert module.parse_steps('["a"]') == ["a"]
    try:
        module.parse_steps('{"not":"array"}')
    except ValueError as exc:
        assert "JSON array" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_sensitive_values_are_rejected_and_legacy_output_is_redacted() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "memory.json"
        secret = "password=super-secret-value"
        proc = run_cli(store, "add_success", "2026-06-18", "bad", '["keep"]', secret)
        assert proc.returncode != 0
        assert secret not in proc.stderr
        assert not store.exists()

        store.write_text(
            '[{"type":"success","date":"2026-06-18","situation":"legacy token=sk-1234567890abcdef","steps":[],"result":"done"}]\n',
            encoding="utf-8",
        )
        proc = run_cli(store, "search", "legacy")
        assert proc.returncode == 0, proc.stderr
        assert "sk-1234567890abcdef" not in proc.stdout
        assert "[REDACTED]" in proc.stdout

        proc = run_cli(store, "add_fail", "2026-06-18", "local", '["http://127.0.0.1:8080"]', "failed")
        assert proc.returncode != 0

        proc = run_cli(store, "add_success", "2026-06-18", "short key", '["sk-test"]', "failed")
        assert proc.returncode != 0

        store.write_text('[{"type":"success","api_key":"short-value","situation":"legacy"}]\n', encoding="utf-8")
        proc = run_cli(store, "search", "legacy")
        assert "short-value" not in proc.stdout
        assert "[REDACTED]" in proc.stdout


def main() -> int:
    test_search_and_query_alias()
    test_add_fail_and_invalid_steps_are_safe()
    test_module_parse_steps_requires_array()
    test_sensitive_values_are_rejected_and_legacy_output_is_redacted()
    print("PASS: harness-memory tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
