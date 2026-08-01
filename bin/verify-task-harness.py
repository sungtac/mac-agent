#!/usr/bin/env python3
"""Deterministic handoff and verification harness for verify-task-v2.

The harness owns repository facts.  Agents receive the small JSON package it
produces instead of re-discovering the repository or interpreting long logs.
It intentionally does not use an LLM.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


SCHEMA = "edge_agent.verify_task_handoff.v1"
MAX_RULE_CHARS = 2400
MAX_DIFF_CHARS = 40000
MAX_FILE_CHARS = 12000

SENSITIVE = (
    re.compile(r"(^|/)(\.env(?:\.|$)|secrets?|credentials?|private|keys?)(/|$|\.)", re.I),
    re.compile(r"(^|/)\.github/workflows/", re.I),
    re.compile(r"(^|/)(auth|authentication|authorization|security|permissions?)(/|\.|$)", re.I),
    re.compile(r"(^|/)(config|configuration|deploy|deployment|infra|infrastructure|migrations?)(/|\.|$)", re.I),
    re.compile(r"(^|/)(Dockerfile|docker-compose(?:\.|$)|Makefile|.*\.lock$)", re.I),
)
PROTECTED = (
    re.compile(r"(^|/)\.git(/|$)"),
    re.compile(r"(^|/)(\.env(?:\.|$)|secrets?|credentials?|.*\.pem$|.*\.key$)", re.I),
)
PATH_RE = re.compile(r"(?<![A-Za-z0-9_])(?:\.?[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.(?:[A-Za-z0-9_-]+)(?![A-Za-z0-9_])")
FULL_REQUEST_RE = re.compile(r"(?:전체\s*트랙|full\s*track|full\s*validation|전체\s*검증|전수\s*검증)", re.I)
DESTRUCTIVE_RE = re.compile(r"(?:drop\s+table|truncate|delete\s+from|파괴적|대량\s*삭제|데이터\s*삭제)", re.I)
API_BREAK_RE = re.compile(r"(?:breaking\s*change|하위\s*호환\s*파괴|공개\s*api.*(?:삭제|변경)|remove.*(?:api|endpoint))", re.I)


def run(command: list[str], cwd: Path, timeout: int = 20) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def git(cwd: Path, *args: str, timeout: int = 20) -> tuple[int, str, str]:
    return run(["git", *args], cwd, timeout)


def safe_rel(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def status_files(cwd: Path) -> list[str]:
    code, out, _ = git(cwd, "status", "--porcelain=v1", "-z")
    if code != 0:
        return []
    result: list[str] = []
    for item in out.split("\0"):
        if not item:
            continue
        value = item[3:] if len(item) >= 3 else item
        if " -> " in value:
            value = value.rsplit(" -> ", 1)[-1]
        # The harness owns its run handoff directory. It is created during
        # init/snapshot and must not be reported as a user change.
        if value == ".verify" or value.startswith(".verify/"):
            continue
        normalized = safe_rel(value)
        # Handoff artifacts are verification state, not task changes.  They
        # must never trigger light→full promotion or enter the review diff.
        if normalized == ".verify" or normalized.startswith(".verify/runs/"):
            continue
        result.append(normalized)
    return sorted(set(result))


def head_state(cwd: Path) -> dict[str, Any]:
    _, head, _ = git(cwd, "rev-parse", "HEAD")
    _, branch, _ = git(cwd, "branch", "--show-current")
    return {"head_sha": head.strip(), "branch": branch.strip(), "files_changed": status_files(cwd)}


def task_paths(task: str) -> list[str]:
    found = []
    for value in PATH_RE.findall(task or ""):
        path = safe_rel(value)
        if path not in found and not path.startswith("http"):
            found.append(path)
    return found


def is_sensitive(path: str) -> bool:
    normalized = safe_rel(path)
    return any(pattern.search(normalized) for pattern in SENSITIVE)


def is_protected(path: str) -> bool:
    normalized = safe_rel(path)
    return any(pattern.search(normalized) for pattern in PROTECTED)


def discover_rules(cwd: Path, files: list[str]) -> tuple[list[str], str]:
    candidates = [cwd / "CLAUDE.md", cwd / "AGENTS.md"]
    for parent in cwd.parents:
        candidates.extend([parent / "CLAUDE.md", parent / "AGENTS.md"])
        if parent == parent.parent:
            break
    candidates.extend(sorted(cwd.glob(".claude/rules/**/*.md")))
    seen: set[Path] = set()
    paths: list[str] = []
    snippets: list[str] = []
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen or not candidate.is_file() or not os.access(candidate, os.R_OK):
            continue
        seen.add(candidate)
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")[:MAX_RULE_CHARS]
        except OSError:
            continue
        rel = str(candidate.relative_to(cwd)) if candidate.is_relative_to(cwd) else str(candidate)
        paths.append(rel)
        snippets.append(f"--- {rel} ---\n{content}")
    return paths, "\n\n".join(snippets)


def test_commands(cwd: Path) -> list[str]:
    commands: list[str] = []
    package = cwd / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
            for name in ("test", "lint", "typecheck"):
                if name in scripts:
                    commands.append(f"npm run {name}")
        except (OSError, json.JSONDecodeError):
            pass
    if (cwd / "pytest.ini").exists() or (cwd / "tests").is_dir():
        commands.append("python3 -m pytest")
    if (cwd / "Makefile").is_file():
        try:
            targets = re.findall(r"^([A-Za-z0-9_.-]+):", (cwd / "Makefile").read_text(encoding="utf-8", errors="replace"), re.M)
            for target in ("test", "lint", "typecheck"):
                if target in targets:
                    commands.append(f"make {target}")
        except OSError:
            pass
    return list(dict.fromkeys(commands))


def classify(task: str, files: list[str], explicit_full: bool = False) -> dict[str, Any]:
    flags = {
        "user_requested_full": bool(explicit_full or FULL_REQUEST_RE.search(task or "")),
        "sensitive_path": any(is_sensitive(path) for path in files),
        "protected_path": any(is_protected(path) for path in files),
        "migration": any(re.search(r"(^|/)migrations?(/|\.|$)", path, re.I) for path in files),
        "deployment_or_infra": any(re.search(r"(^|/)(\.github/workflows|deploy|deployment|infra|infrastructure|Dockerfile|docker-compose)(/|\.|$)", path, re.I) for path in files),
        "new_dependency": any(Path(path).name in {"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod"} for path in files),
        "public_api_breaking": bool(API_BREAK_RE.search(task or "")),
        "destructive_data_change": bool(DESTRUCTIVE_RE.search(task or "")),
    }
    reasons = [name for name, value in flags.items() if value]
    light = len(files) <= 3 and not reasons
    return {"track": "light" if light else "full", "reasons": reasons, **flags}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_metric(run_dir: Path, record: dict[str, Any]) -> None:
    path = run_dir / "metrics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def init_run(cwd: Path, task: str, run_dir: Path, explicit_full: bool) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    state = head_state(cwd)
    files = sorted(set(state["files_changed"]) | set(task_paths(task)))
    rules, rule_text = discover_rules(cwd, files)
    commands = test_commands(cwd)
    policy = classify(task, files, explicit_full)
    preflight_result = preflight(cwd)
    relevant = [path for path in files if (cwd / path).is_file()]
    task_id = hashlib.sha256(f"{cwd}\0{task}".encode()).hexdigest()[:16]
    task_record = {"schema": SCHEMA, "task_id": task_id, "task": task, "cwd": str(cwd), "run_dir": str(run_dir)}
    repository = {**state, "status": "clean" if not state["files_changed"] else "dirty"}
    package = {
        "schema": SCHEMA,
        "task_id": task_id,
        "run_dir": str(run_dir),
        "repository": repository,
        "relevant_files": relevant,
        "protected_files": [path for path in files if is_protected(path)],
        "applicable_rules": rules,
        "rules_text": rule_text,
        "test_commands": commands,
        "policy": policy,
        "preflight": preflight_result,
        # Compatibility aliases for the existing nano-gate adapter.  New
        # callers should use the snake_case fields above.
        "cwdExists": True,
        "intendedFiles": files,
        "sensitivePath": policy["sensitive_path"],
        "context_text": "\n".join([
            f"repo={cwd}", f"branch={state['branch'] or '(detached)'}", f"head={state['head_sha']}",
            f"changed_files={','.join(state['files_changed']) or '(none)'}",
            f"relevant_files={','.join(relevant) or '(none)'}",
            f"rules={','.join(rules) or '(none)'}",
            f"tests={'; '.join(commands) or '(not detected)'}",
        ]),
    }
    write_json(run_dir / "task.json", task_record)
    write_json(run_dir / "repository-state.json", repository)
    (run_dir / "applicable-rules.md").write_text(rule_text + "\n", encoding="utf-8")
    (run_dir / "relevant-files.txt").write_text("\n".join(relevant) + "\n", encoding="utf-8")
    (run_dir / "protected-files.txt").write_text("\n".join(package["protected_files"]) + "\n", encoding="utf-8")
    write_json(run_dir / "investigation.json", {"relevant_files": relevant, "applicable_rules": rules, "risks": policy["reasons"], "unknowns": [], "recommended_tests": commands})
    write_json(run_dir / "track.json", policy)
    append_metric(run_dir, {
        "task_id": task_id, "track": policy["track"], "agent": "harness", "role": "preflight_and_package",
        "round": 0, "model": None, "effort": None, "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0, "package_bytes": len(json.dumps(package, ensure_ascii=False).encode("utf-8")),
        "package_tokens": max(1, len(json.dumps(package, ensure_ascii=False)) // 4),
    })
    return package


def parse_diff_files(status: list[str]) -> list[str]:
    return sorted(set(status))


def snapshot(cwd: Path, task: str, run_dir: Path, explicit_full: bool) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    state = head_state(cwd)
    code, diff, err = git(cwd, "diff", "--binary", "HEAD", timeout=60)
    if code != 0:
        diff = f"git diff failed: {err}"
    chunks = [diff]
    for path in state["files_changed"]:
        if not (cwd / path).is_file() or "diff --" in diff and path in diff:
            continue
        try:
            content = (cwd / path).read_text(encoding="utf-8", errors="replace")[:MAX_FILE_CHARS]
        except OSError:
            continue
        chunks.append(f"\n=== UNTRACKED OR UNDIFFED FILE: {path} ===\n{content}")
    combined = "".join(chunks)
    truncated = len(combined) > MAX_DIFF_CHARS
    if truncated:
        combined = combined[:MAX_DIFF_CHARS] + "\n...(diff truncated by harness)"
    flags = classify(task, state["files_changed"], explicit_full)
    manifest_added = bool(re.search(r"^\+.*(?:dependencies|devDependencies|install_requires|requires|dependencies:)", diff, re.M | re.I))
    flags["new_dependency"] = flags["new_dependency"] or manifest_added
    flags["public_api_breaking"] = flags["public_api_breaking"] or bool(
        re.search(r"(^|/)(api|routes?|endpoints?|openapi)(/|\.|$)", "\n".join(state["files_changed"]), re.I)
        and re.search(r"^-.*(?:export\s+(?:default\s+)?function|route\(|endpoint|openapi|public\s+api)", diff, re.M | re.I)
    )
    flags["destructive_data_change"] = flags["destructive_data_change"] or bool(
        re.search(r"^[+-].*(?:drop\s+table|truncate|delete\s+from)", diff, re.M | re.I)
    )
    flags["reasons"] = [name for name, value in flags.items() if name not in {"track", "reasons"} and value]
    flags["track"] = "full" if any(flags[name] for name in ("user_requested_full", "sensitive_path", "protected_path", "migration", "deployment_or_infra", "new_dependency", "public_api_breaking", "destructive_data_change")) or len(state["files_changed"]) > 3 else "light"
    if flags["track"] == "full" and "actual_scope" not in flags["reasons"]:
        flags["reasons"] = list(dict.fromkeys(flags["reasons"] + (["actual_file_count"] if len(state["files_changed"]) > 3 else [])))
    write_json(run_dir / "repository-state.json", state)
    (run_dir / "current.diff").write_text(combined, encoding="utf-8")
    write_json(run_dir / "track.json", flags)
    package = {
        "schema": SCHEMA,
        "task_id": run_dir.name,
        "run_dir": str(run_dir),
        "content": combined,
        "diff_path": str(run_dir / "current.diff"),
        "files_changed": state["files_changed"],
        "head_sha": state["head_sha"],
        "policy": flags,
        "package_bytes": len(combined.encode("utf-8")),
        "package_tokens": max(1, len(combined) // 4),
        "truncated": truncated,
        # Compatibility aliases for the existing nano-gate adapter.
        "filesChanged": state["files_changed"],
        "headSha": state["head_sha"],
        "sensitivePath": flags["sensitive_path"],
    }
    append_metric(run_dir, {
        "task_id": run_dir.name, "track": flags["track"], "agent": "harness", "role": "snapshot",
        "round": 0, "model": None, "effort": None, "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0, "package_bytes": package["package_bytes"],
        "package_tokens": package["package_tokens"],
    })
    return package


def preflight(cwd: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    codex = os.environ.get("CODEX_BIN") or shutil.which("codex") or "/opt/homebrew/bin/codex"
    agy = os.environ.get("AGY_BIN") or shutil.which("agy") or str(Path.home() / ".local/bin/agy")
    if not Path(codex).is_file() or not os.access(codex, os.X_OK):
        checks.append({"name": "codex", "ok": False, "reason": "executable unavailable"})
    else:
        code, _, err = run([codex, "login", "status"], cwd, 20)
        checks.append({"name": "codex", "ok": code == 0, "reason": "login status failed" if code else "available"})
    if not Path(agy).is_file() or not os.access(agy, os.X_OK):
        checks.append({"name": "antigravity", "ok": False, "reason": "executable unavailable"})
    else:
        code, _, err = run([agy, "models"], cwd, 20)
        checks.append({"name": "antigravity", "ok": code == 0, "reason": "models check failed" if code else "available"})
    return {"schema": SCHEMA, "ok": all(item["ok"] for item in checks), "issues": "; ".join(f"{item['name']}: {item['reason']}" for item in checks if not item["ok"]), "checks": checks}


def run_tests(cwd: Path, run_dir: Path) -> dict[str, Any]:
    commands = test_commands(cwd)
    if not commands:
        result = {"schema": SCHEMA, "status": "not_run", "commands": [], "failures": [], "full_log_path": str(run_dir / "logs/tests.log")}
        write_json(run_dir / "test-summary.json", result)
        append_metric(run_dir, {"task_id": run_dir.name, "track": None, "agent": "harness", "role": "test_runner", "round": 0, "model": None, "effort": None, "input_tokens": 0, "output_tokens": 0, "package_bytes": 0, "package_tokens": 0})
        return result
    command = commands[0]
    log_path = run_dir / "logs" / "tests.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(command, cwd=cwd, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
        output = proc.stdout or ""
        log_path.write_text(output, encoding="utf-8", errors="replace")
        failures = [line.strip() for line in output.splitlines() if re.search(r"(?:FAIL|ERROR|Error:|failed|FAILED)", line, re.I)][:10]
        status = "passed" if proc.returncode == 0 else "failed"
        result = {"schema": SCHEMA, "status": status, "command": command, "exit_code": proc.returncode, "failures": failures, "full_log_path": str(log_path)}
    except subprocess.TimeoutExpired as exc:
        log_path.write_text((exc.stdout or "") if isinstance(exc.stdout, str) else "", encoding="utf-8")
        result = {"schema": SCHEMA, "status": "error", "command": command, "failures": ["test command timed out"], "full_log_path": str(log_path)}
    write_json(run_dir / "test-summary.json", result)
    append_metric(run_dir, {"task_id": run_dir.name, "track": None, "agent": "harness", "role": "test_runner", "round": 0, "model": None, "effort": None, "input_tokens": 0, "output_tokens": 0, "package_bytes": 0, "package_tokens": 0})
    return result


def snapshot_and_tests(cwd: Path, task: str, run_dir: Path, explicit_full: bool) -> dict[str, Any]:
    package = snapshot(cwd, task, run_dir, explicit_full)
    package["test_summary"] = run_tests(cwd, run_dir)
    return package


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["preflight", "init", "snapshot", "snapshot-tests", "tests"])
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--task", default="")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    cwd = Path(args.cwd).resolve()
    run_dir = Path(args.run_dir).resolve()
    if not cwd.is_dir():
        print(json.dumps({"schema": SCHEMA, "ok": False, "error": "cwd does not exist"}, ensure_ascii=False))
        return 0
    if args.command == "preflight":
        output = preflight(cwd)
    elif args.command == "init":
        output = init_run(cwd, args.task, run_dir, args.full)
    elif args.command == "snapshot-tests":
        output = snapshot_and_tests(cwd, args.task, run_dir, args.full)
    elif args.command == "snapshot":
        output = snapshot(cwd, args.task, run_dir, args.full)
    else:
        output = run_tests(cwd, run_dir)
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
