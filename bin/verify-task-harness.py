#!/usr/bin/env python3
"""Deterministic handoff and verification harness for verify-task-v2.

The harness owns repository facts.  Agents receive the small JSON package it
produces instead of re-discovering the repository or interpreting long logs.
It intentionally does not use an LLM.
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ABSENCE_GUARD_PATH = Path(__file__).with_name("edge_agent_absence_guard.py")
ABSENCE_GUARD_SPEC = importlib.util.spec_from_file_location("edge_agent_absence_guard", ABSENCE_GUARD_PATH)
if ABSENCE_GUARD_SPEC is None or ABSENCE_GUARD_SPEC.loader is None:
    raise RuntimeError(f"absence guard unavailable: {ABSENCE_GUARD_PATH}")
ABSENCE_GUARD = importlib.util.module_from_spec(ABSENCE_GUARD_SPEC)
sys.modules[ABSENCE_GUARD_SPEC.name] = ABSENCE_GUARD
ABSENCE_GUARD_SPEC.loader.exec_module(ABSENCE_GUARD)

IMPROVEMENT_PATH = Path(__file__).with_name("edge_agent_improvement.py")
IMPROVEMENT_SPEC = importlib.util.spec_from_file_location("edge_agent_improvement", IMPROVEMENT_PATH)
if IMPROVEMENT_SPEC is None or IMPROVEMENT_SPEC.loader is None:
    raise RuntimeError(f"improvement task module unavailable: {IMPROVEMENT_PATH}")
IMPROVEMENT = importlib.util.module_from_spec(IMPROVEMENT_SPEC)
sys.modules[IMPROVEMENT_SPEC.name] = IMPROVEMENT
IMPROVEMENT_SPEC.loader.exec_module(IMPROVEMENT)


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
    re.compile(r"(^|/)(README(?:\.[^/]+)?|docs)(/|$)", re.I),
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


def _status_files_and_untracked(cwd: Path) -> tuple[list[str], set[str]]:
    code, out, _ = git(cwd, "status", "--porcelain=v1", "-z")
    if code != 0:
        return [], set()
    result: list[str] = []
    untracked: set[str] = set()
    for item in out.split("\0"):
        if not item:
            continue
        status = item[:2]
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
        # Live daemon state is runtime noise, not a task change. Exclude only
        # the known paths so real changes still affect track and diff review.
        if normalized == "hooks-state" or normalized.startswith("hooks-state/"):
            continue
        if normalized == "discord-bot/repo-locks" or normalized.startswith("discord-bot/repo-locks/"):
            continue
        if re.match(r"^jobs/[^/]+/tmp/", normalized):
            continue
        if normalized in {
            "chrome/chrome-native-host",
            "plugins/.last_inuse_sweep",
            "last-update-result.json",
            ".last-update-result.json",
        }:
            continue
        result.append(normalized)
        if status == "??":
            untracked.add(normalized)
    return sorted(set(result)), untracked


def status_files(cwd: Path) -> list[str]:
    return _status_files_and_untracked(cwd)[0]


def untracked_files(cwd: Path) -> set[str]:
    return _status_files_and_untracked(cwd)[1]


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


def related_test_files(cwd: Path, relevant: list[str]) -> list[str]:
    """Add only existing tests whose basename matches a task file.

    A task can require test changes without naming the test path.  The old
    package exposed only changed files and explicit task paths, so an agent
    could be forbidden from updating the existing matching test.  Keep the
    handoff bounded by selecting basename matches rather than every test in
    the repository.
    """
    stems = {Path(path).stem for path in relevant}
    candidates: list[Path] = []
    candidates.extend(cwd.glob("test_*.py"))
    candidates.extend(cwd.glob("*.test.*"))
    tests_dir = cwd / "tests"
    if tests_dir.is_dir():
        candidates.extend(tests_dir.rglob("test_*.py"))
        candidates.extend(tests_dir.rglob("*.test.*"))
    matches: list[str] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        name = candidate.name
        if name.startswith("test_"):
            candidate_stem = name[5:].split(".", 1)[0]
        else:
            candidate_stem = name.split(".test.", 1)[0]
        if candidate_stem not in stems:
            continue
        relative = safe_rel(str(candidate.relative_to(cwd)))
        if relative not in matches:
            matches.append(relative)
    return sorted(matches)


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
    package_scripts: dict[str, str] = {}
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
            package_scripts = scripts if isinstance(scripts, dict) else {}
            for name in ("test", "lint", "typecheck"):
                if name in package_scripts:
                    commands.append(f"npm run {name}")
        except (OSError, json.JSONDecodeError):
            pass
    make_targets: set[str] = set()
    if (cwd / "Makefile").is_file():
        try:
            make_targets = set(re.findall(r"^([A-Za-z0-9_.-]+):", (cwd / "Makefile").read_text(encoding="utf-8", errors="replace"), re.M))
            for target in ("test", "lint", "typecheck"):
                if target in make_targets:
                    commands.append(f"make {target}")
        except OSError:
            pass

    tests_dir = cwd / "tests"
    has_js_tests = tests_dir.is_dir() and any(tests_dir.rglob("*.test.js"))
    has_python_tests = tests_dir.is_dir() and any(tests_dir.rglob("test_*.py"))
    has_declared_test = "test" in package_scripts or "test" in make_targets

    # Only use pytest when the repository declares pytest configuration and the
    # interpreter can import it. A bare tests/ directory is not evidence that
    # pytest is installed; this repository uses unittest and node --test.
    pytest_configured = (cwd / "pytest.ini").is_file()
    for config_name in ("pyproject.toml", "setup.cfg"):
        config_path = cwd / config_name
        if config_path.is_file():
            try:
                config_text = config_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                config_text = ""
            pytest_configured = pytest_configured or "[tool.pytest" in config_text or "[tool:pytest]" in config_text
    pytest_available = importlib.util.find_spec("pytest") is not None

    if not has_declared_test:
        if pytest_configured and pytest_available:
            commands.append("python3 -m pytest")
        elif has_python_tests:
            commands.append("python3 -m unittest discover -s tests -p 'test_*.py'")
        elif pytest_configured:
            # Preserve an explicit pytest requirement so the missing runner is
            # reported as a deterministic test failure rather than silently
            # skipping the project's tests.
            commands.append("python3 -m pytest")
        if has_js_tests:
            commands.append("node --test tests/*.test.js")
    return list(dict.fromkeys(commands))


def test_command_argv(command: str, cwd: Path) -> list[str]:
    """Parse a harness-owned test command without invoking a shell.

    The command list is generated from bounded repository metadata above.  We
    still keep shell metacharacters inert by using ``shlex.split`` and expand
    only the test globs that the generated Node command needs.
    """
    argv = shlex.split(command)
    expanded: list[str] = []
    for token in argv:
        if glob.has_magic(token):
            pattern = token if os.path.isabs(token) else str(cwd / token)
            matches = sorted(glob.glob(pattern))
            if matches:
                for match in matches:
                    path = Path(match)
                    expanded.append(str(path.relative_to(cwd)) if path.is_relative_to(cwd) else str(path))
                continue
        expanded.append(token)
    return expanded


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


def ensure_improvement_task(result: dict[str, Any], run_dir: Path, *, source: str = "verify-task-harness") -> dict[str, Any]:
    """Convert every failed workflow into a durable, actionable task.

    A blocked/failed result without an improvement handoff is not a complete
    harness result.  The task is idempotent and contains only bounded,
    non-sensitive facts; provider transcripts never enter the task.
    """
    if result.get("passed") is True:
        return result
    task, outcome = IMPROVEMENT.improvement_for_result(
        result,
        source=source,
        root=os.environ.get("EDGE_AGENT_IMPROVEMENT_ROOT") or None,
    )
    result["improvement_task"] = {**task, "record_outcome": outcome}
    write_json(run_dir / "improvement-task.json", result["improvement_task"])
    return result


def append_metric(run_dir: Path, record: dict[str, Any]) -> None:
    path = run_dir / "metrics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        # Full-track research and review calls are intentionally parallel.
        # Lock the append itself so concurrent provider bridges cannot
        # interleave JSONL records or truncate one another's writes.
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_agent_bridge_metric(run_dir: Path) -> None:
    agent = os.environ.get("VERIFY_AGENT")
    if not agent:
        return

    def optional_int(name: str) -> int | None:
        value = os.environ.get(name)
        try:
            return int(value) if value else None
        except ValueError:
            return None

    package_bytes = optional_int("VERIFY_PACKAGE_BYTES")
    append_metric(run_dir, {
        "task_id": os.environ.get("VERIFY_TASK_ID", run_dir.name),
        "track": os.environ.get("VERIFY_TRACK"),
        "agent": agent,
        "role": os.environ.get("VERIFY_ROLE", "harness-bridge"),
        "round": optional_int("VERIFY_ROUND"),
        "model": os.environ.get("VERIFY_MODEL"),
        "effort": os.environ.get("VERIFY_EFFORT"),
        # The Workflow API does not expose provider usage to the script. Keep
        # unknown values null instead of falsely reporting zero usage.
        "input_tokens": optional_int("VERIFY_INPUT_TOKENS"),
        "output_tokens": optional_int("VERIFY_OUTPUT_TOKENS"),
        "cache_read_tokens": optional_int("VERIFY_CACHE_READ_TOKENS"),
        "cache_creation_tokens": optional_int("VERIFY_CACHE_CREATION_TOKENS"),
        "package_bytes": package_bytes or 0,
        "package_tokens": max(1, package_bytes // 4) if package_bytes else None,
        "prefix_fingerprint": os.environ.get("VERIFY_PREFIX_FINGERPRINT"),
    })


def summarize_failure_lines(output: str) -> list[str]:
    failures: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if re.search(r"^(?:✖|not ok\b|FAIL(?:ED|URE)?\b|ERROR\b|Error:|npm ERR!|E\s+|F\s+)", stripped) or re.search(r"(?:No module named|command not found)", stripped, re.I):
            failures.append(stripped)
    return failures[:10]


def init_run(
    cwd: Path,
    task: str,
    run_dir: Path,
    explicit_full: bool,
    preflight_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    state = head_state(cwd)
    files = sorted(set(state["files_changed"]) | set(task_paths(task)))
    rules, rule_text = discover_rules(cwd, files)
    commands = test_commands(cwd)
    policy = classify(task, files, explicit_full)
    preflight_result = preflight_result if preflight_result is not None else preflight(cwd)
    relevant = [path for path in files if (cwd / path).is_file()]
    relevant = list(dict.fromkeys(relevant + related_test_files(cwd, relevant)))
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
    changed_untracked = untracked_files(cwd)
    relevant_path_file = run_dir / "relevant-files.txt"
    try:
        relevant = [safe_rel(line.strip()) for line in relevant_path_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError):
        relevant = task_paths(task)
    if not relevant:
        relevant = task_paths(task)
    relevant = list(dict.fromkeys(relevant))

    relevant_diff = ""
    if relevant:
        relevant_code, relevant_diff, relevant_err = git(cwd, "diff", "--binary", "HEAD", "--", *relevant, timeout=60)
        if relevant_code != 0:
            relevant_diff = f"git diff failed: {relevant_err}"

    def untracked_chunks(paths: list[str]) -> list[str]:
        chunks: list[str] = []
        for path in paths:
            if path not in changed_untracked or not (cwd / path).is_file():
                continue
            try:
                content = (cwd / path).read_text(encoding="utf-8", errors="replace")[:MAX_FILE_CHARS]
            except OSError:
                continue
            chunks.append(f"\n=== UNTRACKED OR UNDIFFED FILE: {path} ===\n{content}")
        return chunks

    chunks = []
    if relevant_diff:
        chunks.append(relevant_diff)
    chunks.extend(untracked_chunks(relevant))
    chunks.append(diff)
    chunks.extend(untracked_chunks(state["files_changed"]))
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
    discovery = ABSENCE_GUARD.discover_local_sources(subject="provider/configuration/capability")
    return {
        "schema": SCHEMA,
        "ok": all(item["ok"] for item in checks),
        "issues": "; ".join(f"{item['name']}: {item['reason']}" for item in checks if not item["ok"]),
        "checks": checks,
        "discovery_evidence": discovery.as_dict(candidate_limit=80),
    }


def run_tests(cwd: Path, run_dir: Path) -> dict[str, Any]:
    commands = test_commands(cwd)
    if not commands:
        result = {"schema": SCHEMA, "status": "not_run", "commands": [], "results": [], "failures": [], "full_log_path": str(run_dir / "logs/tests.log")}
        write_json(run_dir / "test-summary.json", result)
        append_metric(run_dir, {"task_id": run_dir.name, "track": None, "agent": "harness", "role": "test_runner", "round": 0, "model": None, "effort": None, "input_tokens": 0, "output_tokens": 0, "package_bytes": 0, "package_tokens": 0})
        return result
    log_path = run_dir / "logs" / "tests.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    all_failures: list[str] = []
    log_chunks: list[str] = []
    for index, command in enumerate(commands, start=1):
        try:
            argv = test_command_argv(command, cwd)
            proc = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
            output = proc.stdout or ""
            failures = summarize_failure_lines(output)
            command_status = "passed" if proc.returncode == 0 else "failed"
            result = {"command": command, "argv": argv, "status": command_status, "exit_code": proc.returncode, "failures": failures}
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            result = {"command": command, "status": "error", "failures": ["test command timed out"]}
        results.append(result)
        all_failures.extend(f"{command}: {failure}" for failure in result.get("failures", []))
        log_chunks.append(f"=== command {index}: {command} ===\n{output}")

    log_path.write_text("\n\n".join(log_chunks), encoding="utf-8", errors="replace")
    status = "passed" if all(item["status"] == "passed" for item in results) else "failed"
    result = {
        "schema": SCHEMA,
        "status": status,
        "commands": commands,
        "results": results,
        "failures": all_failures[:10],
        "full_log_path": str(log_path),
    }
    if status != "passed":
        task, outcome = IMPROVEMENT.improvement_for_result(
            {"passed": False, "error": "harness_tests_failed", "blocking_issues": [f"failure_count={len(all_failures)}"]},
            source="verify-task-harness.tests",
            root=os.environ.get("EDGE_AGENT_IMPROVEMENT_ROOT") or None,
        )
        result["improvement_task"] = {**task, "record_outcome": outcome}
        write_json(run_dir / "improvement-task.json", result["improvement_task"])
    write_json(run_dir / "test-summary.json", result)
    append_metric(run_dir, {"task_id": run_dir.name, "track": None, "agent": "harness", "role": "test_runner", "round": 0, "model": None, "effort": None, "input_tokens": 0, "output_tokens": 0, "package_bytes": 0, "package_tokens": 0})
    return result


def snapshot_and_tests(cwd: Path, task: str, run_dir: Path, explicit_full: bool) -> dict[str, Any]:
    package = snapshot(cwd, task, run_dir, explicit_full)
    package["test_summary"] = run_tests(cwd, run_dir)
    return package


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["preflight", "init", "snapshot", "snapshot-tests", "tests", "record-agent-metric"])
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
    elif args.command == "record-agent-metric":
        append_agent_bridge_metric(run_dir)
        output = {"schema": SCHEMA, "ok": True, "run_dir": str(run_dir)}
    else:
        output = run_tests(cwd, run_dir)
    if args.command != "record-agent-metric":
        append_agent_bridge_metric(run_dir)
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
