#!/usr/bin/env python3
"""Host-side verify-task-v2 orchestrator.

The Claude Workflow runtime cannot execute local processes directly.  This
entrypoint therefore owns the deterministic harness and invokes the
subscription-backed provider CLIs as ordinary host subprocesses.  Codex is
used for light and full-track implementation; Claude and Antigravity provide
independent reviews for the full track, while Antigravity reviews the light
track.
"""

from __future__ import annotations

import argparse
import difflib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = ROOT / "bin" / "verify-task-harness.py"
SCORE_DISPATCH = ROOT / "workflows" / "lib" / "score-dispatch.sh"
CODEX_EXECUTE_DISPATCH = ROOT / "workflows" / "lib" / "codex-execute-dispatch.sh"
PROFILE_SHA = "825e6f984205537e35701781a0216c9e197e6c6aa9a64214d77bcdc4e5df7793"
HISTORY_SCHEMA = "edge_agent.verify_task_history.v1"
HEADLESS_FILE_CHARS = 12000
HEADLESS_TOTAL_FILE_CHARS = 24000
HEADLESS_RULE_CHARS = 6000
CRITICAL_SECURITY_RE = re.compile(r"security|secret|credential|token|auth|permission|injection|xss|csrf|취약|보안", re.I)
CRITICAL_MIGRATION_RE = re.compile(r"migration|migrations|schema|database|db/|\.sql$|마이그레이션|스키마", re.I)
TEST_FAILURE_RE = re.compile(r"test.*fail|failed.*test|regression|회귀|실패 로그|assert", re.I)


def detect_scope_violation(diff: dict[str, Any], context: dict[str, Any]) -> list[str]:
    relevant_files = context.get("relevant_files", [])
    if not isinstance(relevant_files, list) or not relevant_files:
        return []

    def normalize(path: Any) -> str:
        return str(path).replace("\\", "/").lstrip("./")

    allowed = {normalize(path) for path in relevant_files}
    files_changed = diff.get("files_changed", [])
    if not isinstance(files_changed, list):
        return []
    return [path for path in (normalize(item) for item in files_changed) if path not in allowed]


def delta_prompt_context(findings: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps({"id": item.get("id"), "file": item.get("file"), "anchor": item.get("location") or item.get("anchor"), "symbol": item.get("symbol"), "snippet": item.get("snippet"), "evidence": item.get("evidence"), "title": item.get("title") or item.get("description")}, ensure_ascii=False) for item in findings) or "(이전 라운드 open finding 없음)"


FINDING_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
FINDING_STOPWORDS = {
    "the", "and", "for", "from", "this", "that", "with", "issue", "file",
    "line", "code", "error", "review", "finding", "description", "evidence",
    "def", "class", "function", "return", "const", "let", "var", "public",
    "bug", "fix", "test", "data",
}


def _finding_text(finding: dict[str, Any]) -> str:
    return " ".join(str(finding.get(key) or "") for key in ("location", "anchor", "symbol", "snippet", "code", "evidence", "title", "description"))


def _finding_paths(finding: dict[str, Any]) -> set[str]:
    paths = {str(finding.get("file") or "").strip()}
    for key in ("location", "anchor"):
        value = str(finding.get(key) or "").strip()
        if value:
            paths.add(value.split(":", 1)[0].strip())
    return {
        path.replace("\\", "/").lstrip("./")
        for path in paths
        if path and ("/" in path or "." in path)
    }


def _finding_symbols(finding: dict[str, Any]) -> set[str]:
    symbols = set()
    symbol_text = " ".join(
        str(finding.get(key) or "")
        for key in ("location", "anchor", "symbol", "snippet", "code")
    )
    for token in FINDING_IDENTIFIER_RE.findall(symbol_text):
        if token.lower() not in FINDING_STOPWORDS:
            symbols.add(token)
    return symbols


def _git_path_mapping(cwd: Path) -> dict[str, set[str]] | None:
    try:
        result = subprocess.run(
            ["git", "diff", "-M", "-C", "--name-status", "HEAD"],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        mapping: dict[str, set[str]] = {}
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) >= 3 and fields[0][:1] in {"R", "C"}:
                old_path, new_path = (field.replace("\\", "/").lstrip("./") for field in fields[-2:])
                mapping.setdefault(old_path, set()).add(new_path)
                mapping.setdefault(new_path, set()).add(old_path)
        return mapping
    except Exception:
        return None


def _paths_correspond(previous_paths: set[str], current_paths: set[str], mapping: dict[str, set[str]]) -> bool:
    if previous_paths & current_paths:
        return True
    return any(mapped_path in current_paths for path in previous_paths for mapped_path in mapping.get(path, set()))


def _snippet_similarity(previous: dict[str, Any], current: dict[str, Any]) -> float:
    previous_text = " ".join(str(previous.get(key) or "") for key in ("snippet", "anchor", "evidence"))
    current_text = " ".join(str(current.get(key) or "") for key in ("snippet", "anchor", "evidence"))
    if not previous_text or not current_text:
        return 0.0
    return difflib.SequenceMatcher(None, previous_text, current_text).ratio()


def match_open_finding(issue: dict[str, Any], previous_open: list[dict[str, Any]], cwd: Path) -> dict[str, Any] | None:
    """Return the prior finding matching issue, using host-observed anchors."""
    current_paths = _finding_paths(issue)
    current_symbols = _finding_symbols(issue)
    mapping = _git_path_mapping(cwd)
    for previous in previous_open:
        previous_paths = _finding_paths(previous)
        paths_match = _paths_correspond(previous_paths, current_paths, mapping or {})
        common_symbols = current_symbols & _finding_symbols(previous)
        if paths_match and common_symbols:
            return previous
        if mapping is None and common_symbols:
            return previous
        if _snippet_similarity(previous, issue) >= 0.72 and (paths_match or not previous_paths or not current_paths):
            return previous
    return None

def is_critical_delta_issue(issue: dict[str, Any], diff_text: str, tests: dict[str, Any]) -> bool:
    evidence = " ".join(str(issue.get(key, "")) for key in ("description", "issue", "evidence", "location", "file"))
    if tests.get("status") not in {"passed", "not_run"} and TEST_FAILURE_RE.search(evidence): return True
    if CRITICAL_SECURITY_RE.search(evidence) and (CRITICAL_SECURITY_RE.search(diff_text) or issue.get("evidence")): return True
    return bool(CRITICAL_MIGRATION_RE.search(evidence) and CRITICAL_MIGRATION_RE.search(diff_text))


def load_harness():
    spec = importlib.util.spec_from_file_location("verify_task_harness", HARNESS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"하네스 모듈을 불러올 수 없음: {HARNESS_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HARNESS = load_harness()


def resolve_binary(env_name: str, candidates: list[Path], command: str) -> str | None:
    override = os.environ.get(env_name)
    if override and Path(override).is_file() and os.access(override, os.X_OK):
        return override
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    found = shutil_which(command)
    return found


def ensure_git_worktree(cwd: Path) -> dict[str, Any]:
    if (cwd / ".git").exists():
        return {"initialized": False}
    if not cwd.is_dir():
        return {"initialized": False, "error": "not_a_directory"}

    try:
        gitignore = cwd / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "*.log\n"
                "*.jsonl\n"
                "*.jsonl.lock\n"
                "cache/\n"
                "*cache*/\n"
                "sessions/\n"
                "session-*/\n"
                "*token*\n"
                "*credential*\n"
                "*secret*\n"
                "*.key\n"
                "*.pem\n"
                "backups/\n"
                "downloads/\n"
                "paste-cache/\n"
                "shell-snapshots/\n"
                "telemetry/\n"
                ".verify/\n",
                encoding="utf-8",
            )
        commands = [
            ["git", "init"],
            ["git", "add", "-A"],
            [
                "git",
                "-c", "user.email=verify-task@local",
                "-c", "user.name=verify-task-gate",
                "commit", "-m", "chore: initialize git worktree for verify-task gate",
            ],
        ]
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=cwd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            if completed.returncode != 0:
                return {
                    "initialized": False,
                    "error": "git_init_failed",
                    "detail": completed.stderr.strip(),
                }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"initialized": False, "error": "git_init_failed", "detail": str(exc)}
    return {"initialized": True}


def shutil_which(command: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_candidates(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    values: list[Any] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character not in "[{":
            index += 1
            continue
        try:
            value, consumed = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            index += 1
            continue
        values.append(value)
        # Skip the complete top-level value. Otherwise nested objects and
        # arrays become later candidates and can hide the provider envelope.
        index += consumed
    return values


def last_json(text: str) -> Any | None:
    values = json_candidates(text)
    return values[-1] if values else None


def nested_provider_result(value: Any) -> tuple[Any, dict[str, Any]]:
    """Unwrap Claude --output-format json while retaining usage metadata."""
    usage: dict[str, Any] = {}
    if not isinstance(value, dict):
        return value, usage
    for key in ("usage", "token_usage"):
        if isinstance(value.get(key), dict):
            usage = value[key]
            break
    result = value.get("result")
    if isinstance(result, str):
        parsed = last_json(result)
        if parsed is not None:
            return parsed, usage
    return value, usage


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120] or "call"


def _inject_discovery_evidence(
    value: dict[str, Any],
    source: str,
    note: str,
    omitted_files: list[str] | None = None,
) -> None:
    evidence_entry: dict[str, Any] = {"source": source, "note": note}
    if omitted_files is not None:
        evidence_entry["omitted_files"] = omitted_files
    existing = value.get("discovery_evidence")
    if isinstance(existing, list):
        existing.append(evidence_entry)
    else:
        value["discovery_evidence"] = [evidence_entry]


class HostOrchestrator:
    def __init__(self, task: str, cwd: Path, run_dir: Path, max_rounds: int, explicit_full: bool):
        self.task = task
        self.cwd = cwd.resolve()
        self.run_dir = run_dir.resolve()
        self.max_rounds = max(1, max_rounds)
        self.explicit_full = explicit_full
        self.preflight_result: dict[str, Any] | None = None
        self.history: list[dict[str, Any]] = []
        self.claude = resolve_binary(
            "CLAUDE_BIN",
            [Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")],
            "claude",
        )
        self.codex = resolve_binary(
            "CODEX_BIN",
            [Path.home() / ".local/bin/codex", Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex")],
            "codex",
        )
        self.agy = resolve_binary(
            "AGY_BIN",
            [Path.home() / ".local/bin/agy", Path("/opt/homebrew/bin/agy"), Path("/usr/local/bin/agy")],
            "agy",
        )

    def log_dir(self) -> Path:
        path = self.run_dir / "logs" / "host"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def history_path(self) -> Path:
        configured = os.environ.get("VERIFY_TASK_HISTORY_FILE")
        return Path(configured).expanduser() if configured else Path.home() / ".claude" / "verify-task-v2-history.jsonl"

    def append_history(self, result: dict[str, Any]) -> None:
        """Append a redacted terminal result without interleaving concurrent runs."""
        payload = {
            "schema": HISTORY_SCHEMA,
            "run_id": self.run_dir.name,
            "task_sha256": hashlib.sha256(self.task.encode("utf-8")).hexdigest(),
            "cwd": str(self.cwd),
            "track": result.get("track") or result.get("tier"),
            "passed": result.get("passed") is True,
            "error": result.get("error"),
            "improvement_task_id": (result.get("improvement_task") or {}).get("task_id"),
            "round": result.get("round"),
            "completed_at": now_utc(),
        }
        path = self.history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.seek(0)
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        continue
                    try:
                        existing = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(f"검증 이력 원장 손상: line={line_number}") from exc
                    if not isinstance(existing, dict):
                        raise RuntimeError(f"검증 이력 원장 레코드 형식 오류: line={line_number}")
                stream.seek(0, os.SEEK_END)
                stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def persist_result(self, result: dict[str, Any]) -> None:
        try:
            HARNESS.ensure_improvement_task(result, self.run_dir, source="verify-task-orchestrator")
        except Exception as exc:
            result["passed"] = False
            result["error"] = "improvement_task_persist_failed"
            result["improvement_error"] = type(exc).__name__
        try:
            self.append_history(result)
        except (OSError, RuntimeError) as exc:
            # A successful workflow without a durable history record is not a
            # successful workflow. Mutate the shared result so every caller
            # and the CLI exit code fail closed together.
            result["passed"] = False
            result["error"] = "history_persist_failed"
            result["history_error"] = str(exc)
            if "improvement_task" not in result:
                try:
                    HARNESS.ensure_improvement_task(result, self.run_dir, source="verify-task-orchestrator")
                except Exception as improvement_exc:
                    result["error"] = "improvement_task_persist_failed"
                    result["improvement_error"] = type(improvement_exc).__name__
        HARNESS.write_json(self.run_dir / "final-verdict.json", result)
        if result.get("blocking_issues"):
            HARNESS.write_json(self.run_dir / "blocking-issues.json", result["blocking_issues"])

    def record_metric(self, agent: str, role: str, prompt: str, usage: dict[str, Any] | None = None) -> None:
        usage = usage or {}
        def usage_value(*names: str) -> int | None:
            for name in names:
                value = usage.get(name)
                if isinstance(value, int):
                    return value
            return None

        HARNESS.append_metric(self.run_dir, {
            "task_id": self.run_dir.name,
            "track": None,
            "agent": agent,
            "role": role,
            "round": None,
            "model": "sonnet" if agent == "claude" else None,
            "effort": "medium" if agent == "claude" else None,
            "input_tokens": usage_value("input_tokens", "prompt_tokens"),
            "output_tokens": usage_value("output_tokens", "completion_tokens"),
            "cache_read_tokens": usage_value("cache_read_input_tokens", "cache_read_tokens"),
            "cache_creation_tokens": usage_value("cache_creation_input_tokens", "cache_creation_tokens"),
            "package_bytes": len(prompt.encode("utf-8")),
            "package_tokens": max(1, len(prompt) // 4),
            "prefix_fingerprint": PROFILE_SHA,
        })

    def process(self, argv: list[str], role: str, timeout: int, log_name: str, env: dict[str, str] | None = None) -> tuple[int, str]:
        log_path = self.log_dir() / f"{safe_name(log_name)}.log"
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.cwd,
                env=merged_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                output, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    output, _ = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    output, _ = process.communicate()
                output = (output or "") + f"\nTIMEOUT after {timeout}s\n"
                code = 124
            else:
                code = process.returncode
        except OSError as exc:
            output = f"{type(exc).__name__}: {exc}"
            code = 127
        log_path.write_text(output or "", encoding="utf-8", errors="replace")
        return code, output or ""

    def preflight(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        if not self.claude:
            checks.append({"name": "claude", "ok": False, "reason": "executable unavailable"})
        else:
            code, output = self.process([self.claude, "--version"], "preflight", 20, "claude-version")
            checks.append({"name": "claude", "ok": code == 0, "reason": "available" if code == 0 else output[-500:]})
        provider = HARNESS.preflight(self.cwd)
        checks.extend(provider.get("checks", []))
        return {
            "schema": HARNESS.SCHEMA,
            "ok": all(item.get("ok") for item in checks),
            "issues": "; ".join(f"{item['name']}: {item.get('reason', '')}" for item in checks if not item.get("ok")),
            "checks": checks,
        }

    def init(self) -> dict[str, Any]:
        return HARNESS.init_run(
            self.cwd,
            self.task,
            self.run_dir,
            self.explicit_full,
            preflight_result=self.preflight_result,
        )

    def snapshot_tests(self) -> dict[str, Any]:
        return HARNESS.snapshot_and_tests(self.cwd, self.task, self.run_dir, self.explicit_full)

    def write_prompt(self, role: str, prompt: str) -> Path:
        path = self.log_dir() / f"{safe_name(role)}.prompt.md"
        path.write_text(prompt, encoding="utf-8")
        return path

    def claude_call(self, role: str, prompt: str, expect_json: bool = False) -> dict[str, Any]:
        if not self.claude:
            return {"ok": False, "error": "claude_unavailable"}
        self.write_prompt(role, prompt)
        code, output = self.process(
            [self.claude, "-p", "--model", "sonnet", "--effort", "medium", "--output-format", "json", prompt],
            role,
            900,
            role,
            {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"},
        )
        envelope = last_json(output)
        result, usage = nested_provider_result(envelope)
        self.record_metric("claude", role, prompt, usage)
        if expect_json and not isinstance(result, dict):
            repair_prompt = (
                "[중요] 이전 응답이 요구된 JSON 형식을 지키지 않았다. "
                "지금부터 응답은 예외 없이 아래 지시가 요구하는 JSON 객체 하나만 출력해야 한다. "
                "설명, 요약, 코드블록, 추가 텍스트를 절대 포함하지 마라. "
                "응답 전체가 그 자체로 유효한 JSON이어야 한다.\n\n"
                + prompt
            )
            retry_role = f"{role}-retry"
            retry_code, retry_output = self.process(
                [self.claude, "-p", "--model", "sonnet", "--effort", "medium", "--output-format", "json", repair_prompt],
                retry_role,
                900,
                retry_role,
                {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"},
            )
            retry_envelope = last_json(retry_output)
            retry_result, retry_usage = nested_provider_result(retry_envelope)
            self.record_metric("claude", retry_role, repair_prompt, retry_usage)
            if isinstance(retry_result, dict):
                code, output, result, usage = retry_code, retry_output, retry_result, retry_usage
        try:
            importlib.reload(HARNESS.ABSENCE_GUARD)
        except Exception as exc:
            return {"ok": False, "error": "absence_guard_reload_failed", "message": f"{type(exc).__name__}: {exc}"}
        try:
            HARNESS.ABSENCE_GUARD.validate_provider_payload(result if result is not None else output)
        except HARNESS.ABSENCE_GUARD.UnsupportedAbsenceClaim as exc:
            return {"ok": False, "error": "unsupported_absence_claim", "message": str(exc), "discovery_required": True}
        answer: dict[str, Any] = {"ok": code == 0, "raw": output[-4000:], "usage": usage}
        if expect_json:
            if isinstance(result, dict):
                answer.update(result)
            else:
                answer.update({"ok": False, "error": "claude_json_result_missing"})
        return answer

    def dispatch(
        self,
        tool: str,
        role: str,
        prompt: str,
        schema_kind: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headless_omitted: list[str] = []
        if tool == "agy":
            prompt, headless_omitted = self.headless_evidence_prompt(prompt, context, include_files=schema_kind == "research")
        prompt_path = self.write_prompt(role, prompt)
        binary = self.codex if tool == "codex" else self.agy
        if not binary:
            return {"dispatchFailed": True, "dispatchFailureReason": f"{tool} executable unavailable"}
        env = {
            "VERIFY_METRICS_FILE": str(self.run_dir / "metrics.jsonl"),
            "VERIFY_TASK_ID": self.run_dir.name,
            "VERIFY_AGENT": tool,
            "VERIFY_ROLE": role,
        }
        code, output = self.process(["/bin/bash", str(SCORE_DISPATCH), tool, str(prompt_path), schema_kind], role, 900, role, env)
        value = last_json(output)
        self.record_metric(tool, role, prompt)
        if code != 0 or not isinstance(value, dict):
            return {"dispatchFailed": True, "dispatchFailureReason": f"{tool} dispatch failed: {output[-1000:]}"}
        _inject_discovery_evidence(
            value,
            source="host_policy_omission" if headless_omitted else "orchestrator_role_exempt",
            note=(
                "orchestrator withheld these file contents under its own headless evidence budget/protection policy before this response was generated; any absence language about them reflects that host policy, not an unverified provider claim"
                if headless_omitted
                else "this dispatch is part of the verify-task orchestrator's own research/plan/review/self-check workflow, not a capability/credential/configuration absence claim the guard is meant to catch; quoted code, error strings, or task instructions may incidentally contain absence-sounding phrases"
            ),
            omitted_files=headless_omitted if headless_omitted else None,
        )
        try:
            importlib.reload(HARNESS.ABSENCE_GUARD)
        except Exception as exc:
            return {"dispatchFailed": True, "dispatchFailureReason": "absence_guard_reload_failed", "message": f"{type(exc).__name__}: {exc}"}
        try:
            HARNESS.ABSENCE_GUARD.validate_provider_payload(value)
        except HARNESS.ABSENCE_GUARD.UnsupportedAbsenceClaim as exc:
            return {"dispatchFailed": True, "dispatchFailureReason": "unsupported_absence_claim", "discoveryRequired": True, "message": str(exc)}
        return value

    def execute_codex(self, role: str, prompt: str) -> dict[str, Any]:
        prompt_path = self.write_prompt(role, prompt)
        if not self.codex:
            return {"ok": False, "dispatchFailed": True, "message": "codex executable unavailable"}
        env = {
            "CODEX_EXECUTE_AUDIT_FILE": str(self.run_dir / "codex-execute-events.jsonl"),
        }
        code, output = self.process(["/bin/bash", str(CODEX_EXECUTE_DISPATCH), str(self.cwd), str(prompt_path)], role, 1800, role, env)
        self.record_metric("codex", role, prompt)
        value = last_json(output)
        if code != 0 or not isinstance(value, dict):
            return {"ok": False, "dispatchFailed": True, "message": output[-2000:]}
        _inject_discovery_evidence(
            value,
            source="orchestrator_role_exempt",
            note="this is a real-code-implementation/self-report role (codex-execute/codex-fix), not a capability/credential/configuration absence claim the guard is meant to catch; quoted diff content or test-failure prose may incidentally contain absence-sounding phrases",
        )
        try:
            importlib.reload(HARNESS.ABSENCE_GUARD)
        except Exception as exc:
            return {"ok": False, "dispatchFailed": True, "error": "absence_guard_reload_failed", "message": f"{type(exc).__name__}: {exc}"}
        try:
            HARNESS.ABSENCE_GUARD.validate_provider_payload(value)
        except HARNESS.ABSENCE_GUARD.UnsupportedAbsenceClaim as exc:
            return {"ok": False, "dispatchFailed": True, "error": "unsupported_absence_claim", "discoveryRequired": True, "message": str(exc)}
        return value

    def context_text(self, context: dict[str, Any]) -> str:
        return json.dumps({
            "relevant_files": context.get("relevant_files", []),
            "applicable_rules": context.get("applicable_rules", []),
            "test_commands": context.get("test_commands", []),
            "policy": context.get("policy", {}),
            "discovery_evidence": context.get("preflight", {}).get("discovery_evidence", {}),
        }, ensure_ascii=False)

    def headless_evidence_prompt(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
        include_files: bool = True,
    ) -> tuple[str, list[str]]:
        """Turn an Antigravity call into a bounded, tool-free evidence review.

        `agy -p` cannot ask for command permission.  Supplying repository paths
        therefore causes a silent/empty dispatch when the model tries to
        inspect them.  The host already owns the deterministic repository
        snapshot, so include only explicitly relevant, non-protected files.
        """
        sections = [
            "[HEADLESS EVIDENCE CONTRACT]",
            "이 호출은 headless 독립 조사/리뷰다. Bash, command, git, Read, Grep 또는 어떤 도구도 호출하지 마라.",
            "아래에 제공된 증거만 사용하고, 증거가 부족하면 추측하지 말고 findings/notes에 부족함을 명시하라.",
            "파일을 수정하거나 테스트를 실행하거나 저장소를 재탐색하지 마라.",
            "",
            prompt,
        ]
        if not context:
            return "\n".join(sections), []

        snippets: list[str] = []
        omitted: list[str] = []
        remaining = HEADLESS_TOTAL_FILE_CHARS
        for relative in context.get("relevant_files", []):
            if not include_files:
                omitted.append(str(relative))
                continue
            if remaining <= 0:
                omitted.append(str(relative))
                continue
            try:
                candidate = (self.cwd / relative).resolve()
                if not candidate.is_file() or not candidate.is_relative_to(self.cwd):
                    omitted.append(str(relative))
                    continue
                if HARNESS.is_protected(str(candidate.relative_to(self.cwd))):
                    omitted.append(str(relative))
                    continue
                content = candidate.read_text(encoding="utf-8", errors="replace")[:min(HEADLESS_FILE_CHARS, remaining)]
                snippets.append(f"--- {relative} ---\n{content}")
                remaining -= len(content)
            except (OSError, ValueError):
                omitted.append(str(relative))
        sections.extend(["", "[HEADLESS RELEVANT FILE EVIDENCE]"])
        sections.append("\n\n".join(snippets) if snippets else "(직접 제공 가능한 관련 파일 내용 없음)")
        if omitted:
            sections.extend(["", "[OMITTED BY HOST POLICY]", ", ".join(omitted)])
        rules_text = context.get("rules_text")
        if isinstance(rules_text, str) and rules_text:
            sections.extend(["", "[APPLICABLE RULE TEXT]", rules_text[:HEADLESS_RULE_CHARS]])
        return "\n".join(sections), omitted

    def implement_prompt(self, context: dict[str, Any], feedback: str = "") -> str:
        return f"""[agent profile v1.0.0]\n영구 역할: 정밀 구현 및 검증 엔지니어\n현재 persona: implementer\n\n작업 디렉토리: {self.cwd}\n허용된 파일만 수정하고 저장소 전체를 탐색하지 마.\n\n[원 작업]\n{self.task}\n\n[하네스 패키지]\n{self.context_text(context)}\n\n[추가 수정 지시]\n{feedback or '(첫 구현)'}\n\n실제로 파일을 수정한 뒤 변경 파일과 실행한 테스트를 짧게 설명해."""

    def light_review_prompt(self, context: dict[str, Any], diff: dict[str, Any], test_summary: dict[str, Any]) -> str:
        return f"""[agent profile v1.0.0]\n영구 역할: 독립 조사관이자 레드팀 검증자\n현재 persona: auditor\n\n경량 트랙의 독립 차단 리뷰어다. 점수는 매기지 않는다. 실제 diff, 하네스 트랙 판정, 테스트 요약만 근거로 판단한다.\n\n[원 작업]\n{self.task}\n[허용 파일/정책]\n{self.context_text(context)}\n[실제 변경사항]\n{diff.get('content', '')}\n[트랙]\n{json.dumps(diff.get('policy', {}), ensure_ascii=False)}\n[테스트 요약]\n{json.dumps(test_summary, ensure_ascii=False)}\n\nJSON으로만 답해: {{"verdict":"pass|changes_required|full_track_required","blocking_issues":[]}}"""

    def _legacy_review_prompt(self, context: dict[str, Any], diff: dict[str, Any], test_summary: dict[str, Any], plan: str, role: str) -> str:
        persona = "communicator" if role == "claude" else "auditor"
        identity = "제한된 구현자 또는 독립 리뷰어" if role == "claude" else "독립 조사관이자 레드팀 검증자"
        return f"""[agent profile v1.0.0]\n영구 역할: {identity}\n현재 persona: {persona}\n\n너는 독립 코드 리뷰어다. 점수는 매기지 않는다. 다른 리뷰어의 의견을 전제로 삼지 말고 제공된 패키지만 검토한다. 테스트를 다시 실행하지 마라.\n\n[원 작업]\n{self.task}\n[최종 계획]\n{plan or '(계획 없음)'}\n[허용 파일/적용 규칙]\n{self.context_text(context)}\n[실제 변경사항]\n{diff.get('content', '')}\n[테스트 요약]\n{json.dumps(test_summary, ensure_ascii=False)}\n\n문제가 있으면 file, symbol, line_start, line_end, issue, evidence, required_fix, required_test, blocking을 포함한다.\nJSON으로만 답해: {{"hasBlockingIssue":false,"issues":[],"checks":[{{"name":"test-summary","status":"passed","evidence":"하네스 요약 확인"}}]}}"""

    def review_prompt(self, context: dict[str, Any], diff: dict[str, Any], test_summary: dict[str, Any], plan: str, role: str) -> str:
        persona = "communicator" if role == "claude" else "auditor"
        identity = "제한된 구현자 또는 독립 리뷰어" if role == "claude" else "독립 조사관이자 레드팀 검증자"
        scope_violations = detect_scope_violation(diff, context)
        scope_notice = ""
        if scope_violations:
            scope_notice = "\n[하네스 사전검사: 범위 위반 감지됨]\n" + ", ".join(scope_violations) + "\n"
        checklist = """[완성도 체크리스트]
1. 지시서가 허용한 파일 목록과 실제 diff의 파일 목록이 정확히 일치하는가? 벗어난 파일이 있으면 blocking이다.
2. 지시서가 요구한 신규/수정 결과물이 diff에 실제로 존재하는가? 요구했는데 없으면 blocking이다.
3. 지시서가 명시한 검증 명령이 실행됐는가? test_summary.status가 not_run인데 검증 명령이 지시서에 명시돼 있었다면 blocking이다. 애초에 검증 명령이 지시되지 않았다면 blocking이 아니다.
4. finding에서 참조하는 필드/스키마가 실제 코드/데이터 구조와 일치하는가? 존재하지 않는 필드를 참조하는 코드를 놓치지 않았는가.
5. 지시한 변경 내용과 실제 diff가 대응하는가? 지시서가 말한 파일이 그대로고 엉뚱한 파일만 바뀌어 있으면 blocking이다."""
        return f"""[agent profile v1.0.0]\n영구 역할: {identity}\n현재 persona: {persona}\n{scope_notice}\n{checklist}\n\n너는 독립 코드 리뷰어다. 점수는 매기지 않는다. 다른 리뷰어의 의견을 전제로 삼지 말고 제공된 패키지만 검토한다. 테스트를 다시 실행하지 마라.\n\n[원 작업]\n{self.task}\n[최종 계획]\n{plan or '(계획 없음)'}\n[허용 파일/적용 규칙]\n{self.context_text(context)}\n[실제 변경사항]\n{diff.get('content', '')}\n[테스트 요약]\n{json.dumps(test_summary, ensure_ascii=False)}\n\n문제가 있으면 file, symbol, line_start, line_end, issue, evidence, required_fix, required_test, blocking을 포함한다.\nJSON으로만 답해: {{\"hasBlockingIssue\":false,\"issues\":[],\"checks\":[{{\"name\":\"test-summary\",\"status\":\"passed\",\"evidence\":\"하네스 요약 확인\"}}]}}"""

    def research_prompt(self, context: dict[str, Any], focus: str, instruction: str) -> str:
        persona = "red-team" if focus == "risks-tests" else "researcher"
        return f"""[agent profile v1.0.0]\n영구 역할: 독립 조사관이자 레드팀 검증자\n현재 persona: {persona}\n\n코드를 수정하거나 계획을 확정하지 말고 아래 초점만 조사한다. 저장소 컨텍스트에 없는 사실은 추측하지 않는다. 구성·자격·기능이 없다고 판단할 때는 반드시 하네스의 discovery_evidence 또는 searched_scopes를 결과에 포함한다.\n[작업]\n{self.task}\n[컨텍스트]\n{self.context_text(context)}\n[조사 초점]\n{instruction}\n\nJSON으로만 답해: {{"focus":"{focus}","findings":"","evidence":[{{"source":"","fact":"","relevance":""}}],"discovery_evidence":[],"risks":[],"testImplications":[]}}"""

    def plan_prompt(self, context: dict[str, Any], research: list[dict[str, Any]]) -> str:
        return f"""[agent profile v1.0.0]\n영구 역할: 정밀 구현 및 검증 엔지니어\n현재 persona: architect\n\nAntigravity 조사와 하네스 패키지만 보고 Codex가 바로 실행할 구현 계획을 작성한다. 허용 파일, 의존성 순서, 통합 단계, 테스트 명령을 포함한다.\n[원 작업]\n{self.task}\n[하네스 패키지]\n{self.context_text(context)}\n[조사 요약]\n{json.dumps(research, ensure_ascii=False)}\n\nJSON으로만 답해: {{"needsClarification":false,"clarifyingQuestions":"","plan":"구현 순서, 파일 소유권, 통합 및 테스트"}}"""

    def record_delta_report(self, round_number: int, issues: list[dict[str, Any]], deferred: list[dict[str, Any]], parent: str | None) -> str | None:
        report_path: str | None = None
        try:
            head_result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.cwd, text=True, capture_output=True, check=False)
            if head_result.returncode != 0:
                return None
            head = head_result.stdout.strip()
            findings = [{"id": item.get("id") or f"verify-{round_number}-{index + 1}", "severity": "medium" if index >= len(issues) else "blocker", "category": "security" if CRITICAL_SECURITY_RE.search(str(item)) else "correctness", "location": str(item.get("location") or item.get("file") or f"commit:{head}"), "title": str(item.get("title") or item.get("description") or item.get("issue") or "Delta Review finding")[:180], "evidence": str(item.get("evidence") or item.get("description") or "host review evidence"), "remediation": str(item.get("required_fix") or "Review and correct the finding."), "status": "deferred" if index >= len(issues) else "open"} for index, item in enumerate([*issues, *deferred])]
            report = {"schema_version": "edge_agent.code_review_report.v1", "review_id": "verify-task-" + hashlib.sha256(str(self.task).encode()).hexdigest()[:24] + f"-round-{round_number}", "status": "CHANGES_REQUIRED" if issues else "AI_APPROVED", "target": {"scope": "diff", "head_sha": head, "repository": str(self.cwd)}, "round": round_number, "parent_report_key": parent, "findings": findings, "checks": [{"name": "verify-task-review", "status": "passed"}]}
            if not issues: report["approval"] = {"provider": "verify-task-host", "reviewed_head_sha": head, "decision_reason": "bounded Delta Review completed"}
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
                json.dump(report, handle, ensure_ascii=False); report_path = handle.name
            subprocess.run(["node", str(ROOT / "workflows/lib/code-review-store.js"), "--record", report_path], cwd=self.cwd, env={**os.environ, "EDGE_AGENT_CODE_REVIEW_STATE_ROOT": str(self.run_dir / "code-review-state")}, text=True, capture_output=True, check=True)
            return report["review_id"] + "::" + head
        except (OSError, subprocess.CalledProcessError, TypeError, ValueError):
            return None
        finally:
            if report_path:
                try:
                    Path(report_path).unlink(missing_ok=True)
                except OSError:
                    pass

    def run_light(self, context: dict[str, Any]) -> dict[str, Any]:
        feedback = ""
        for round_number in range(1, self.max_rounds + 1):
            implementation = self.execute_codex(f"light-implement-round-{round_number}", self.implement_prompt(context, feedback))
            if implementation.get("dispatchFailed") or implementation.get("ok") is False:
                return {"passed": False, "tier": "light", "error": "codex_implementation_failed", "round": round_number}
            verification = self.snapshot_tests()
            policy = verification.get("policy", {})
            if policy.get("track") == "full" or len(verification.get("files_changed", [])) > 3:
                return self.run_full(context, verification, baseline=True)
            review = self.dispatch("agy", f"light-eval-round-{round_number}", self.light_review_prompt(context, verification, verification.get("test_summary", {})), "light-eval", context)
            # Distinct filename from run_full's review-antigravity.json: a
            # light->full escalation reuses this same run_dir, and would
            # otherwise silently overwrite the light-track review that was
            # the very evidence for escalating right as it happens. The
            # record also lives in self.history/final-verdict.json either
            # way; this keeps the standalone snapshot file honest too.
            HARNESS.write_json(self.run_dir / "review-antigravity-light.json", review)
            self.history.append({"tier": "light", "round": round_number, "review": review, "test_summary": verification.get("test_summary")})
            if review.get("dispatchFailed"):
                return {"passed": False, "tier": "light", "error": "light_review_failed", "round": round_number}
            issues = review.get("blocking_issues", [])
            verdict = review.get("verdict")
            if verdict == "full_track_required":
                return self.run_full(context, verification, baseline=True)
            if verification.get("test_summary", {}).get("status") == "passed" and verdict == "pass" and not issues:
                return {"passed": True, "tier": "light", "round": round_number}
            if round_number == self.max_rounds:
                return {"passed": False, "tier": "light", "error": "changes_required", "round": round_number, "blocking_issues": issues}
            feedback = json.dumps(issues, ensure_ascii=False)
        return {"passed": False, "tier": "light", "error": "light_exhausted"}

    def run_full(self, context: dict[str, Any], verification: dict[str, Any] | None = None, baseline: bool = False) -> dict[str, Any]:
        plan = ""
        if verification is None:
            research_foci = [
                ("repository", "관련 파일, 호출 흐름, 컨벤션과 재사용 구현 조사"),
                ("dependencies", "사용 중인 라이브러리·프로토콜·API의 현재 제약 조사"),
                ("risks-tests", "실패 경로, 보안·호환성 위험과 필요한 테스트 조사"),
            ]
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(self.dispatch, "agy", f"research-{focus}", self.research_prompt(context, focus, instruction), "research", context) for focus, instruction in research_foci]
                research = [future.result() for future in futures]
            if any(item.get("dispatchFailed") for item in research):
                return {"passed": False, "tier": "full", "error": "research_failed"}
            HARNESS.write_json(self.run_dir / "investigation.json", {
                "relevant_files": context.get("relevant_files", []),
                "applicable_rules": context.get("applicable_rules", []),
                "risks": context.get("policy", {}).get("reasons", []),
                "unknowns": [],
                "recommended_tests": context.get("test_commands", []),
                "research": research,
            })
            plan_result = self.dispatch("codex", "codex-plan", self.plan_prompt(context, research), "plan")
            if plan_result.get("dispatchFailed") or not plan_result.get("plan"):
                return {"passed": False, "tier": "full", "error": "plan_failed"}
            if plan_result.get("needsClarification"):
                return {"passed": False, "tier": "full", "error": "needs_clarification", "questions": plan_result.get("clarifyingQuestions", "")}
            plan = plan_result["plan"]
            execution = self.execute_codex("codex-execute", f"[원 작업]\n{self.task}\n[계획]\n{plan}\n[허용 파일]\n{json.dumps(context.get('relevant_files', []), ensure_ascii=False)}")
            if execution.get("ok") is False or execution.get("dispatchFailed"):
                return {"passed": False, "tier": "full", "error": "execution_failed"}
            verification = self.snapshot_tests()
        tests = verification.get("test_summary", {})
        if tests.get("status") not in ("passed", "not_run"):
            return {"passed": False, "tier": "full", "error": "tests_failed", "test_summary": tests}

        previous_open: list[dict[str, Any]] = []
        parent_report_key: str | None = None
        for round_number in range(1, self.max_rounds + 1):
            delta_suffix = f"\n[DELTA REVIEW 이전 open finding]\n{delta_prompt_context(previous_open)}\n2라운드 이후 목록 밖 신규 이슈는 Critical 증거가 없으면 deferred로 기록하라." if round_number > 1 else ""
            claude_prompt = self.review_prompt(context, verification, tests, plan, "claude") + delta_suffix
            agy_prompt = self.review_prompt(context, verification, tests, plan, "antigravity") + delta_suffix
            with ThreadPoolExecutor(max_workers=2) as pool:
                claude_future = pool.submit(self.claude_call, f"full-review-round-{round_number}", claude_prompt, True)
                agy_future = pool.submit(self.dispatch, "agy", f"antigravity-review-round-{round_number}", agy_prompt, "review", context)
                claude_review = claude_future.result()
                agy_review = agy_future.result()
            HARNESS.write_json(self.run_dir / "review-claude.json", claude_review)
            HARNESS.write_json(self.run_dir / "review-antigravity.json", agy_review)
            self.history.append({"tier": "full", "round": round_number, "claude_review": claude_review, "antigravity_review": agy_review, "test_summary": tests})
            issues: list[dict[str, Any]] = []
            if not claude_review.get("ok") or "hasBlockingIssue" not in claude_review:
                issues.append({"description": "Claude 독립 리뷰 결과를 받지 못함", "blocking": True, "source": "claude"})
            else:
                issues.extend({**issue, "source": "claude"} for issue in claude_review.get("issues", []) if issue.get("blocking", False))
                issues.extend({"description": f"Claude check failed: {check.get('name', 'unknown')}", "evidence": check.get("evidence", ""), "blocking": True, "source": "claude"} for check in claude_review.get("checks", []) if check.get("status") not in {"passed", "pass"})
            if agy_review.get("dispatchFailed") or "hasBlockingIssue" not in agy_review:
                issues.append({"description": "Antigravity 독립 리뷰 결과를 받지 못함", "blocking": True, "source": "antigravity"})
            else:
                issues.extend({**issue, "source": "antigravity"} for issue in agy_review.get("issues", []) if issue.get("blocking", False))
                issues.extend({"description": f"Antigravity check failed: {check.get('name', 'unknown')}", "evidence": check.get("evidence", ""), "blocking": True, "source": "antigravity"} for check in agy_review.get("checks", []) if check.get("status") not in {"passed", "pass"})
            deferred: list[dict[str, Any]] = []
            if round_number > 1:
                diff_text = str(verification.get("diff", verification.get("content", "")))
                allowed = []
                for issue in issues:
                    matched = match_open_finding(issue, previous_open, self.cwd)
                    if matched:
                        allowed.append({**issue, "matched_open_finding_id": matched.get("id")})
                    elif is_critical_delta_issue(issue, diff_text, tests): allowed.append(issue)
                    else: deferred.append({**issue, "blocking": False, "status": "deferred"})
                issues = allowed
            parent_report_key = self.record_delta_report(round_number, issues, deferred, parent_report_key)
            previous_open = [*issues, *deferred]
            if not issues:
                return {"passed": True, "tier": "full", "round": round_number, "wasEscapeHatch": baseline}
            if round_number == self.max_rounds:
                return {"passed": False, "tier": "full", "error": "changes_required", "round": round_number, "blocking_issues": issues}
            fix = self.execute_codex("codex-fix", f"[원 작업]\n{self.task}\n[현재 차단 이슈]\n{json.dumps(issues, ensure_ascii=False)}\n각 이슈를 수정하고 허용 파일 밖은 건드리지 마.")
            if fix.get("ok") is False or fix.get("dispatchFailed"):
                return {"passed": False, "tier": "full", "error": "fix_failed", "blocking_issues": issues}
            verification = self.snapshot_tests()
            tests = verification.get("test_summary", {})
            if tests.get("status") not in ("passed", "not_run"):
                return {"passed": False, "tier": "full", "error": "tests_failed", "test_summary": tests}
        return {"passed": False, "tier": "full", "error": "full_exhausted"}

    def run(self) -> dict[str, Any]:
        if not self.cwd.is_dir():
            return {"passed": False, "error": "invalid_git_worktree", "cwd": str(self.cwd)}
        if not (self.cwd / ".git").exists():
            init_result = ensure_git_worktree(self.cwd)
            if not init_result.get("initialized"):
                return {
                    "passed": False,
                    "error": "invalid_git_worktree",
                    "cwd": str(self.cwd),
                    "git_init_error": init_result.get("error"),
                }
        self.run_dir.mkdir(parents=True, exist_ok=True)
        preflight = self.preflight()
        self.preflight_result = preflight
        HARNESS.write_json(self.run_dir / "preflight.json", preflight)
        if not preflight["ok"]:
            result = {"passed": False, "error": "preflight_failed", "preflight": preflight, "run_dir": str(self.run_dir)}
            self.persist_result(result)
            return result
        context = self.init()
        if context.get("preflight", {}).get("ok") is False:
            result = {"passed": False, "error": "harness_preflight_failed", "preflight": context.get("preflight"), "run_dir": str(self.run_dir)}
            self.persist_result(result)
            return result
        if context.get("policy", {}).get("track") == "light" and not self.explicit_full:
            result = self.run_light(context)
        else:
            result = self.run_full(context)
        result["track"] = result.get("tier", context.get("policy", {}).get("track"))
        result["run_dir"] = str(self.run_dir)
        result["history"] = self.history
        self.persist_result(result)
        return result


def update_session_state(session_id: str | None, cwd: Path, result: dict[str, Any]) -> None:
    if not session_id:
        return
    state_root = Path(os.environ.get("VERIFY_TASK_STATE_ROOT", str(Path.home() / ".claude/hooks-state/verify-task-v2")))
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id) or "unknown-session"
    path = state_root / f"{safe}.json"
    state_root.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    passed = result.get("passed") is True

    # A session id is not a capability to unlock an arbitrary checkout. Keep
    # the original session state when a successful invocation targets another
    # cwd; the edit gate will then remain closed for the current session. A
    # failed mismatched attempt may still be recorded for diagnostics, but it
    # must not replace the session's original cwd.
    original_cwd = state.get("cwd")
    same_cwd = True
    if isinstance(original_cwd, str) and original_cwd:
        try:
            same_cwd = Path(original_cwd).resolve() == cwd.resolve()
        except OSError:
            same_cwd = original_cwd == str(cwd)
    if original_cwd and not same_cwd and passed:
        return

    state.update({
        "session_id": session_id,
        "cwd": str(cwd) if same_cwd else original_cwd,
        "status": "workflow_completed" if passed else "workflow_failed",
        "workflow_passed": passed,
        "workflow_path": str(Path(__file__).resolve()),
        "workflow_name": "verify-task-orchestrator",
        "run_dir": result.get("run_dir"),
        "completed_at": now_utc(),
    })
    if original_cwd and not same_cwd:
        state["attempted_cwd"] = str(cwd)
        state["error"] = "session_cwd_mismatch"
    with tempfile.NamedTemporaryFile("w", dir=state_root, prefix=f".{safe}.", suffix=".tmp", delete=False, encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task")
    parser.add_argument("--task-file")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--run-dir")
    parser.add_argument("--max-rounds", type=int, default=2)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--session-id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if bool(args.task) == bool(args.task_file):
        parser.error("--task 또는 --task-file 중 하나만 지정해야 합니다")
    task = Path(args.task_file).read_text(encoding="utf-8") if args.task_file else args.task
    cwd = Path(args.cwd).resolve()
    task_id = hashlib.sha256(f"{cwd}\0{task}".encode()).hexdigest()[:16]
    run_dir = Path(args.run_dir).resolve() if args.run_dir else cwd / ".verify" / "runs" / task_id
    runner = HostOrchestrator(task, cwd, run_dir, args.max_rounds, args.full)
    runner.preflight_result = None
    if args.dry_run:
        if not cwd.is_dir():
            result = {"passed": False, "error": "invalid_git_worktree", "cwd": str(cwd), "run_dir": str(run_dir)}
        elif not (cwd / ".git").exists() and not ensure_git_worktree(cwd).get("initialized"):
            result = {"passed": False, "error": "invalid_git_worktree", "cwd": str(cwd), "run_dir": str(run_dir)}
        else:
            context = runner.init()
            result = {"passed": True, "dry_run": True, "track": context.get("policy", {}).get("track"), "context": context, "run_dir": str(run_dir)}
        runner.persist_result(result)
    else:
        result = runner.run()
    update_session_state(args.session_id, cwd, result)
    print(json.dumps({"finalVerdict": result, "runDir": str(run_dir)}, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
