#!/usr/bin/env python3
"""Local tests for skills/command-registry/command_registry.py."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "command_registry.py"


def load_module():
    spec = importlib.util.spec_from_file_location("command_registry_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def run_cli(store: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["COMMAND_REGISTRY_STORE"] = str(store)
    return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=env)


def test_unknown_valid_and_blacklisted_flow() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "registry.json"
        command = "python3 scripts/example.py --json"

        proc = run_cli(store, "check", command)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "UNKNOWN"

        proc = run_cli(store, "update_success", command)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "RECORDED_SUCCESS"

        proc = run_cli(store, "check", " python3   scripts/example.py   --json ")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "VALID"

        proc = run_cli(store, "update_fail", command, "missing helper", "python3 scripts/replacement.py --json")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "RECORDED_FAIL"

        proc = run_cli(store, "check", command)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "BLACKLISTED -> 대체: python3 scripts/replacement.py --json"


def test_invalid_inputs_are_safe() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "registry.json"
        proc = run_cli(store, "update_success", "   ")
        assert proc.returncode != 0
        assert "ERROR:" in proc.stderr
        assert not store.exists()

        proc = run_cli(store, "update_fail", "bad", "reason", "   ")
        assert proc.returncode != 0
        assert "replacement command must not be empty" in proc.stderr
        assert not store.exists()


def test_module_check_prefers_blacklist_over_verified() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "registry.json"
        module.update_success("openclaw gateway restart", path=store)
        module.update_fail("openclaw gateway restart", "must use safe helper", "scripts/safe_gateway_restart.sh --approved 90", path=store)
        assert module.check_command("openclaw gateway restart", path=store) == "BLACKLISTED -> 대체: scripts/safe_gateway_restart.sh --approved 90"


def test_sensitive_values_are_rejected_without_echoing() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "registry.json"
        secret = "token=sk-1234567890abcdef"
        proc = run_cli(store, "update_success", secret)
        assert proc.returncode != 0
        assert secret not in proc.stderr
        assert not store.exists()

        proc = run_cli(store, "update_fail", "bad", "api_key=super-secret-value", "safe replacement")
        assert proc.returncode != 0
        assert "super-secret-value" not in proc.stderr
        assert not store.exists()

        proc = run_cli(store, "update_success", "curl -H ghp_test-token https://example.com")
        assert proc.returncode != 0
        assert "ghp_test-token" not in proc.stderr

        module = load_module()
        try:
            module.write_store({"verified": [{"api_key": "short-value"}], "blacklisted": []}, store)
        except ValueError as exc:
            assert "sensitive" in str(exc)
        else:
            raise AssertionError("expected structured secret rejection")


def main() -> int:
    test_unknown_valid_and_blacklisted_flow()
    test_invalid_inputs_are_safe()
    test_module_check_prefers_blacklist_over_verified()
    test_sensitive_values_are_rejected_without_echoing()
    print("PASS: command-registry tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
