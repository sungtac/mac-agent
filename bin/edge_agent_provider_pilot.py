#!/usr/bin/env python3
"""Run one explicitly approved provider canary in a clean worktree.

The default action is a read-only plan.  ``--execute`` additionally requires
``--confirm-live-provider`` and never prints or persists provider output.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "bin" / "edge-agent-provider.sh"
USAGE_GATE = ROOT / "workflows" / "lib" / "usage-preflight-gate.sh"
CAPABILITY_PREFLIGHT = ROOT / "bin" / "edge_agent_capability_preflight.py"
MAX_PROMPT_BYTES = 128 * 1024
MAX_TIMEOUT_SECONDS = 30 * 60

IMPROVEMENT_PATH = ROOT / "bin" / "edge_agent_improvement.py"
IMPROVEMENT_SPEC = importlib.util.spec_from_file_location("edge_agent_improvement", IMPROVEMENT_PATH)
if IMPROVEMENT_SPEC is None or IMPROVEMENT_SPEC.loader is None:
    raise RuntimeError(f"improvement task module unavailable: {IMPROVEMENT_PATH}")
IMPROVEMENT = importlib.util.module_from_spec(IMPROVEMENT_SPEC)
sys.modules[IMPROVEMENT_SPEC.name] = IMPROVEMENT
IMPROVEMENT_SPEC.loader.exec_module(IMPROVEMENT)


def _run_git(workdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/git", "-C", str(workdir), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def _clean_worktree(workdir: Path) -> tuple[bool, str]:
    probe = _run_git(workdir, "rev-parse", "--is-inside-work-tree")
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return False, "workdir is not a Git worktree"
    status = _run_git(workdir, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        return False, "worktree status could not be confirmed"
    if status.stdout.strip():
        return False, "worktree must be clean before a provider pilot"
    return True, "clean Git worktree"


def _prompt_check(prompt: Path) -> tuple[bool, str]:
    if not prompt.is_file():
        return False, "prompt file does not exist"
    try:
        size = prompt.stat().st_size
    except OSError:
        return False, "prompt file size could not be confirmed"
    if size == 0:
        return False, "prompt file is empty"
    if size > MAX_PROMPT_BYTES:
        return False, f"prompt file exceeds {MAX_PROMPT_BYTES} bytes"
    return True, f"non-empty prompt file <= {MAX_PROMPT_BYTES} bytes"


def _capability(provider: str) -> tuple[bool, str]:
    name = {"claude": "claude_provider", "codex": "codex_provider", "agy": "antigravity_provider"}[provider]
    try:
        result = subprocess.run(
            [sys.executable, str(CAPABILITY_PREFLIGHT), "--format", "json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError):
        return False, "capability preflight failed"
    for item in payload if isinstance(payload, list) else []:
        if isinstance(item, dict) and item.get("capability") == name:
            state = item.get("state")
            return state == "available", f"{name}: {state}"
    return False, f"capability was not reported: {name}"


def _usage_gate(provider: str, *, allow_unmetered: bool = False) -> tuple[bool, str]:
    if provider == "agy":
        return (allow_unmetered, "no numeric usage gate; explicit --allow-unmetered-provider required")
    try:
        result = subprocess.run(
            ["bash", str(USAGE_GATE), provider],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "usage preflight timed out or could not start"
    decision = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    lowered = decision.casefold()
    if not decision.startswith("PROCEED"):
        return False, decision or "usage preflight returned no decision"
    if "gate skipped" in lowered or "창 잔여" not in decision:
        if allow_unmetered:
            return True, "usage window was not readable; explicit --allow-unmetered-provider override"
        return False, "usage preflight did not confirm a readable provider window"
    return True, decision


def build_plan(provider: str, prompt_file: str | Path, workdir: str | Path, *, allow_unmetered: bool = False) -> dict[str, Any]:
    prompt = Path(prompt_file).expanduser().resolve()
    target = Path(workdir).expanduser().resolve()
    checks: list[dict[str, Any]] = []
    checks.append({"name": "entrypoint", "ok": ENTRYPOINT.is_file() and os.access(ENTRYPOINT, os.X_OK), "detail": str(ENTRYPOINT)})
    prompt_ok, prompt_detail = _prompt_check(prompt)
    checks.append({"name": "prompt", "ok": prompt_ok, "detail": prompt_detail})
    clean_ok, clean_detail = _clean_worktree(target) if target.is_dir() else (False, "workdir does not exist")
    checks.append({"name": "worktree", "ok": clean_ok, "detail": clean_detail})
    capability_ok, capability_detail = _capability(provider)
    checks.append({"name": "capability", "ok": capability_ok, "detail": capability_detail})
    usage_ok, usage_detail = _usage_gate(provider, allow_unmetered=allow_unmetered)
    checks.append({"name": "usage_gate", "ok": usage_ok, "detail": usage_detail})
    return {
        "ok": all(bool(item["ok"]) for item in checks),
        "provider": provider,
        "prompt_file": str(prompt),
        "workdir": str(target),
        "checks": checks,
        "execution_policy": "no provider process starts without --execute --confirm-live-provider",
    }


def attach_improvement_task(plan: dict[str, Any]) -> dict[str, Any]:
    """Turn a blocked plan into a durable next task instead of a dead end."""
    if plan.get("ok") is True or plan.get("improvement_task"):
        return plan
    failed = [item for item in plan.get("checks", []) if not item.get("ok")]
    names = ", ".join(str(item.get("name") or "unknown") for item in failed[:8]) or "unknown"
    details = [
        f"blocked_checks={names}",
        f"provider={plan.get('provider', 'unknown')}",
        f"workdir_clean={next((item.get('ok') for item in plan.get('checks', []) if item.get('name') == 'worktree'), False)}",
    ]
    category = "usage" if any(item.get("name") == "usage_gate" for item in failed) else "process"
    try:
        task, outcome = IMPROVEMENT.record_blocker(
            root=os.environ.get("EDGE_AGENT_IMPROVEMENT_ROOT") or None,
            source="edge-agent-provider-pilot",
            category=category,
            summary=f"{plan.get('provider', 'provider')} 파일럿이 {names} 검사에서 차단됨",
            evidence=details,
            next_action="차단된 사실의 원인을 확인하고 최소 범위의 개선을 구현한 뒤 파일럿 계획을 재실행한다.",
            acceptance="개선 작업이 원장에 queued/in_progress로 기록되고 재검증 계획이 통과될 때까지 provider 실행을 시작하지 않는다.",
        )
        plan["improvement_task"] = {**task, "record_outcome": outcome}
    except Exception as exc:
        plan["improvement_task_error"] = type(exc).__name__
    return plan


def _run_provider(provider: str, prompt: Path, workdir: Path, timeout: int) -> dict[str, Any]:
    env = os.environ.copy()
    env["EDGE_AGENT_PROVIDER_CANARY"] = "1"
    env["EDGE_AGENT_PROVIDER_MODE"] = "pilot"
    with tempfile.TemporaryDirectory(prefix="edge-agent-provider-pilot-state-") as state_root:
        env["EDGE_AGENT_SESSION_ROOT"] = state_root
        # Keep provider output off the Python heap.  The output is intentionally
        # never returned or persisted; only its byte count and digest are kept.
        with tempfile.TemporaryFile(mode="w+b") as output_file:
            try:
                process = subprocess.Popen(
                    [str(ENTRYPOINT), provider, str(prompt), str(workdir)],
                    cwd=ROOT,
                    env=env,
                    stdout=output_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                return {
                    "ok": False,
                    "returncode": None,
                    "timed_out": False,
                    "provider_output_sha256": "",
                    "provider_output_bytes": 0,
                    "changed_files": [],
                    "raw_output": None,
                    "error": type(exc).__name__,
                }
            try:
                process.communicate(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                timed_out = True
            returncode = process.returncode
            output_file.flush()
            output_file.seek(0)
            digest_builder = hashlib.sha256()
            output_bytes = 0
            while chunk := output_file.read(1024 * 1024):
                digest_builder.update(chunk)
                output_bytes += len(chunk)
    digest = digest_builder.hexdigest()
    status = _run_git(workdir, "status", "--porcelain", "--untracked-files=all")
    changed = [line[3:] for line in status.stdout.splitlines() if len(line) >= 4] if status.returncode == 0 else []
    diff_check = _run_git(workdir, "diff", "--check") if status.returncode == 0 else None
    diff_check_ok = diff_check is not None and diff_check.returncode == 0
    return {
        "ok": returncode == 0 and not timed_out and status.returncode == 0 and diff_check_ok,
        "returncode": returncode,
        "timed_out": timed_out,
        "provider_output_sha256": digest,
        "provider_output_bytes": output_bytes,
        "changed_files": changed,
        "worktree_status_ok": status.returncode == 0,
        "diff_check_ok": diff_check_ok,
        "raw_output": None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or run one bounded provider canary")
    parser.add_argument("--provider", choices=("claude", "codex", "agy"), required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--execute", action="store_true", help="start the provider process")
    parser.add_argument("--confirm-live-provider", action="store_true", help="explicitly approve provider usage/cost")
    parser.add_argument(
        "--allow-unmetered-provider",
        action="store_true",
        help="explicitly allow execution when the provider usage window is unavailable (never overrides a known low quota)",
    )
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.timeout > MAX_TIMEOUT_SECONDS:
        parser.error(f"--timeout must be <= {MAX_TIMEOUT_SECONDS} seconds")
    if args.execute and not args.confirm_live_provider:
        payload = {
            "ok": False,
            "provider": args.provider,
            "execution_policy": "no provider process starts without --execute --confirm-live-provider",
            "errors": ["--confirm-live-provider is required for execution"],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else payload)
        return 2
    plan = build_plan(args.provider, args.prompt_file, args.workdir, allow_unmetered=args.allow_unmetered_provider)
    if not plan["ok"]:
        attach_improvement_task(plan)
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2) if args.json else plan)
        return 0 if plan["ok"] else 1
    if not plan["ok"]:
        print(json.dumps(plan, ensure_ascii=False, indent=2) if args.json else plan)
        return 1
    result = _run_provider(args.provider, Path(args.prompt_file).expanduser().resolve(), Path(args.workdir).expanduser().resolve(), args.timeout)
    payload = {**plan, "execution": result}
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else payload)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
