#!/usr/bin/env python3
"""Goal-level completion gate for the multi-agent operating system.

This is the final stop gate, not another progress report.  A goal can only be
marked complete when every required domain has fresh passing evidence and no
open improvement task remains.  Any new failed check is persisted as an
improvement task and keeps the goal open for the next repair cycle.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any, Iterable


SCHEMA = "edge_agent.completion_harness.v1"
EVENT_SCHEMA = "edge_agent.completion_event.v1"
DEFAULT_GOAL = "multi-agent-os"
REQUIRED_DOMAINS = (
    "telegram_canary",
    "security_cost",
    "canonical_parity",
    "lifecycle",
    "regression",
)
TERMINAL_TASK_STATUSES = {"completed", "resolved", "closed", "superseded"}


def _root() -> Path:
    return Path(
        os.environ.get(
            "EDGE_AGENT_COMPLETION_ROOT",
            str(Path.home() / ".edge-agent" / "completion"),
        )
    ).expanduser()


def _safe(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text or any(char in text for char in "/\\\x00") or len(text) > 160:
        raise ValueError(f"unsafe {field}")
    return text


def _bounded(value: object, limit: int = 1200) -> str:
    return " ".join(str(value or "").split())[:limit]


class CompletionError(RuntimeError):
    pass


class CompletionStore:
    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root).expanduser() if root is not None else _root()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.state_path = self.root / "goal-state.json"
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / "completion.lock"

    def _locked(self):
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor

    def _read(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write(self, payload: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".goal-", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            os.chmod(self.state_path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _event(self, event: dict[str, Any]) -> None:
        with self.events_path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        os.chmod(self.events_path, 0o600)

    def init_goal(self, goal_id: str, objective: str, *, domains: Iterable[str] = REQUIRED_DOMAINS) -> dict[str, Any]:
        goal_id = _safe(goal_id, "goal_id")
        selected = tuple(dict.fromkeys(str(item) for item in domains))
        if not selected or any(item not in REQUIRED_DOMAINS for item in selected):
            raise CompletionError("goal domains must be known required domains")
        descriptor = self._locked()
        try:
            current = self._read()
            if current and current.get("goal_id") == goal_id:
                return current
            payload = {
                "schema": SCHEMA,
                "goal_id": goal_id,
                "objective": _bounded(objective, 2000),
                "status": "open",
                "domains": {
                    domain: {"status": "open", "evidence": [], "updated_epoch": time.time()}
                    for domain in selected
                },
                "last_failure": None,
                "created_epoch": time.time(),
                "updated_epoch": time.time(),
            }
            self._write(payload)
            self._event({"schema": EVENT_SCHEMA, "kind": "goal_initialized", "goal_id": goal_id, "epoch": time.time()})
            return payload
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def state(self) -> dict[str, Any] | None:
        return self._read()

    def record_check(self, goal_id: str, domain: str, *, passed: bool, evidence: Iterable[str], blocker: str = "", next_action: str = "") -> dict[str, Any]:
        goal_id = _safe(goal_id, "goal_id")
        if domain not in REQUIRED_DOMAINS:
            raise CompletionError("unknown completion domain")
        descriptor = self._locked()
        try:
            payload = self._read()
            if not payload or payload.get("goal_id") != goal_id:
                raise CompletionError("goal is not initialized")
            if domain not in payload.get("domains", {}):
                raise CompletionError("domain is not registered for this goal")
            evidence_list = [_bounded(item, 600) for item in list(evidence)[:12]]
            item = payload["domains"][domain]
            item.update({
                "status": "passed" if passed else "open",
                "evidence": evidence_list,
                "blocker": "" if passed else _bounded(blocker, 800),
                "next_action": "" if passed else _bounded(next_action, 800),
                "updated_epoch": time.time(),
            })
            payload["status"] = "open"
            payload["updated_epoch"] = time.time()
            if not passed:
                payload["last_failure"] = {"domain": domain, "blocker": _bounded(blocker), "next_action": _bounded(next_action)}
            elif (payload.get("last_failure") or {}).get("domain") == domain:
                payload["last_failure"] = None
            self._write(payload)
            self._event({
                "schema": EVENT_SCHEMA,
                "kind": "check_passed" if passed else "check_failed",
                "goal_id": goal_id,
                "domain": domain,
                "evidence": evidence_list,
                "epoch": time.time(),
            })
            return payload
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def complete(self, goal_id: str, *, unresolved_tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        descriptor = self._locked()
        try:
            payload = self._read()
            if not payload or payload.get("goal_id") != _safe(goal_id, "goal_id"):
                raise CompletionError("goal is not initialized")
            open_domains = [name for name, item in (payload.get("domains") or {}).items() if item.get("status") != "passed"]
            unresolved = unresolved_tasks or []
            if open_domains or unresolved:
                payload["status"] = "open"
                payload["last_failure"] = {
                    "domain": open_domains[0] if open_domains else "improvement_tasks",
                    "blocker": "completion gate blocked",
                    "next_action": "repair every open domain and unresolved improvement task, then rerun the gate",
                }
                payload["updated_epoch"] = time.time()
                self._write(payload)
                self._event({"schema": EVENT_SCHEMA, "kind": "completion_blocked", "goal_id": goal_id, "open_domains": open_domains, "unresolved_task_count": len(unresolved), "epoch": time.time()})
                raise CompletionError(json.dumps({"open_domains": open_domains, "unresolved_task_count": len(unresolved)}, ensure_ascii=False))
            payload["status"] = "complete"
            payload["completed_epoch"] = time.time()
            payload["updated_epoch"] = time.time()
            self._write(payload)
            self._event({"schema": EVENT_SCHEMA, "kind": "goal_completed", "goal_id": goal_id, "epoch": time.time()})
            return payload
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _task_belongs_to_goal(item: dict[str, Any], goal_id: str | None) -> bool:
    if not goal_id:
        return True
    if item.get("goal_id") == goal_id:
        return True
    # Older completion-harness records predate goal_id.  Keep those scoped
    # records for backward compatibility, but do not let unrelated historical
    # tasks (for example a separate nano-threshold review) block this goal.
    return item.get("goal_id") is None and str(item.get("source", "")).startswith("completion-harness")


def unresolved_improvement_tasks(root: str | os.PathLike[str] | None = None, *, goal_id: str | None = None) -> list[dict[str, Any]]:
    ledger_root = Path(root or os.environ.get("EDGE_AGENT_IMPROVEMENT_ROOT", str(Path.home() / ".edge-agent" / "improvements"))).expanduser()
    path = ledger_root / "tasks.jsonl"
    if not path.is_file():
        return []
    unresolved: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            unresolved.append({"status": "malformed"})
            continue
        if isinstance(item, dict) and _task_belongs_to_goal(item, goal_id) and item.get("status", "queued") not in TERMINAL_TASK_STATUSES:
            unresolved.append(item)
    return unresolved


def resolve_completion_tasks(domain: str, evidence: Iterable[str], root: str | os.PathLike[str] | None = None, *, goal_id: str | None = None) -> int:
    """Close completion-harness tasks after the exact domain is revalidated."""
    ledger_root = Path(root or os.environ.get("EDGE_AGENT_IMPROVEMENT_ROOT", str(Path.home() / ".edge-agent" / "improvements"))).expanduser()
    path = ledger_root / "tasks.jsonl"
    if not path.is_file():
        return 0
    evidence_list = [_bounded(item, 500) for item in list(evidence)[:8]]
    ledger_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(ledger_root / "tasks.lock", os.O_CREAT | os.O_RDWR, 0o600)
    changed = 0
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                records.append({"status": "malformed", "raw": line})
                continue
            if isinstance(item, dict):
                acceptance = str(item.get("acceptance", ""))
                matches = (
                    str(item.get("source", "")).startswith("completion-harness")
                    and _task_belongs_to_goal(item, goal_id)
                    and item.get("status", "queued") not in TERMINAL_TASK_STATUSES
                    and (item.get("completion_domain") == domain or f"completion domain {domain}" in acceptance)
                )
                if matches:
                    item["status"] = "completed"
                    item["revalidation_evidence"] = evidence_list
                    item["completed_at"] = time.time()
                    changed += 1
            records.append(item)
        if changed:
            descriptor_tmp, temporary = tempfile.mkstemp(prefix=".tasks-", dir=ledger_root)
            try:
                with os.fdopen(descriptor_tmp, "w", encoding="utf-8") as stream:
                    for item in records:
                        stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                os.chmod(path, 0o600)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    return changed


def run_command(argv: list[str], cwd: Path, *, timeout: int = 300) -> tuple[bool, str]:
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = (result.stdout + "\n" + result.stderr).strip()
    return result.returncode == 0, _bounded(output, 1000)


def check_services(labels: Iterable[str]) -> tuple[bool, list[str]]:
    evidence: list[str] = []
    failed = False
    for label in labels:
        ok, output = run_command(["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"], Path.cwd(), timeout=20)
        running = ok and "state = running" in output
        evidence.append(f"{label}={'running' if running else 'not_running'}")
        failed = failed or not running
    return not failed, evidence


def check_clean_repo(repo: Path, *, allow: Iterable[str] = ()) -> tuple[bool, list[str]]:
    allowed = {str(item).replace("\\", "/") for item in allow}
    try:
        result = subprocess.run(["git", "status", "--porcelain=v1"], cwd=repo, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, [f"git_status_failed={type(exc).__name__}"]
    if result.returncode != 0:
        return False, [f"git_status_failed={_bounded(result.stderr, 500)}"]
    changed = [line for line in result.stdout.splitlines() if line and line[3:] not in allowed]
    return not changed, [f"changed_count={len(changed)}"] + changed[:8]


def canary_evidence_ok(path: Path, *, minimum_rounds: int = 3) -> tuple[bool, list[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, [f"canary_evidence_unreadable={type(exc).__name__}"]
    roles = payload.get("roles") if isinstance(payload, dict) else None
    rounds = payload.get("rounds") if isinstance(payload, dict) else 0
    try:
        round_count = int(rounds)
    except (TypeError, ValueError):
        round_count = -1
    passed = bool(isinstance(payload, dict) and payload.get("passed") is True and isinstance(roles, list) and len(set(roles)) >= 4 and round_count >= minimum_rounds)
    return passed, [f"canary_passed={bool(payload.get('passed')) if isinstance(payload, dict) else False}", f"rounds={rounds}", f"roles={len(set(roles)) if isinstance(roles, list) else 0}"]


def register_failure(store: CompletionStore, goal_id: str, domain: str, *, blocker: str, next_action: str, evidence: list[str]) -> dict[str, Any]:
    """Persist both the failed domain and an idempotent improvement task."""
    store.record_check(goal_id, domain, passed=False, evidence=evidence, blocker=blocker, next_action=next_action)
    improvement_root = Path(os.environ.get("EDGE_AGENT_IMPROVEMENT_ROOT", str(Path.home() / ".edge-agent" / "improvements"))).expanduser()
    task_id = "improve-completion-" + hashlib.sha256(f"{goal_id}|{domain}|{blocker}".encode()).hexdigest()[:24]
    improvement_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = improvement_root / "tasks.jsonl"
    record = {
        "schema": "edge_agent.improvement_task.v1",
        "task_id": task_id,
        "source": "completion-harness",
        "goal_id": goal_id,
        "category": "integration" if domain == "canonical_parity" else "runtime",
        "status": "queued",
        "completion_domain": domain,
        "summary": _bounded(blocker, 800),
        "evidence": evidence[:8],
        "next_action": _bounded(next_action, 800),
        "acceptance": f"completion domain {domain} passes with fresh evidence",
        "discovered_at": time.time(),
    }
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.seek(0)
        existing = [line for line in stream.read().splitlines() if line]
        if not any(task_id in line for line in existing):
            stream.seek(0, os.SEEK_END)
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    os.chmod(path, 0o600)
    return record


def evaluate_goal(store: CompletionStore, goal_id: str) -> dict[str, Any]:
    state = store.state()
    if not state or state.get("goal_id") != goal_id:
        raise CompletionError("goal is not initialized")
    unresolved = unresolved_improvement_tasks(goal_id=goal_id)
    open_domains = [name for name, item in (state.get("domains") or {}).items() if item.get("status") != "passed"]
    return {
        "schema": SCHEMA,
        "goal_id": goal_id,
        "status": "complete" if not open_domains and not unresolved else "open",
        "open_domains": open_domains,
        "unresolved_tasks": len(unresolved),
        "domains": state.get("domains") or {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="force goal-level completion until every domain is proven")
    parser.add_argument("command", choices=("init", "status", "complete", "check-services", "check-canary", "check-repos", "check-command"))
    parser.add_argument("--goal-id", default=DEFAULT_GOAL)
    parser.add_argument("--objective", default="완성된 멀티에이전트 운영체제")
    parser.add_argument("--evidence-file")
    parser.add_argument("--repo", action="append", default=[])
    parser.add_argument("--allow", action="append", default=[])
    parser.add_argument("--domain", choices=("security_cost", "canonical_parity", "regression"))
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    store = CompletionStore()
    try:
        if args.command == "init":
            output = store.init_goal(args.goal_id, args.objective)
        elif args.command == "status":
            output = evaluate_goal(store, args.goal_id)
        elif args.command == "complete":
            output = store.complete(args.goal_id, unresolved_tasks=unresolved_improvement_tasks(goal_id=args.goal_id))
        elif args.command == "check-services":
            labels = ("com.macagent.telegram-claude", "com.multiagent.engine", "com.macagent.telegram-antigravity", "com.macagent.telegram-roda-gemma")
            passed, evidence = check_services(labels)
            if not passed:
                register_failure(store, args.goal_id, "lifecycle", blocker="service not running", evidence=evidence, next_action="restart the failed LaunchAgent with the drain-aware helper")
            else:
                resolve_completion_tasks("lifecycle", evidence, goal_id=args.goal_id)
            output = store.record_check(args.goal_id, "lifecycle", passed=passed, evidence=evidence, blocker="service not running", next_action="restart the failed LaunchAgent with the drain-aware helper")
        elif args.command == "check-canary":
            if not args.evidence_file:
                raise CompletionError("--evidence-file is required")
            passed, evidence = canary_evidence_ok(Path(args.evidence_file).expanduser())
            if not passed:
                register_failure(store, args.goal_id, "telegram_canary", blocker="Telegram 3-round canary evidence is missing or failed", evidence=evidence, next_action="run the canary and persist signed bounded evidence")
            else:
                resolve_completion_tasks("telegram_canary", evidence, goal_id=args.goal_id)
            output = store.record_check(args.goal_id, "telegram_canary", passed=passed, evidence=evidence, blocker="Telegram 3-round canary evidence is missing or failed", next_action="run the canary and persist signed bounded evidence")
        elif args.command == "check-command":
            if not args.domain or not args.argv:
                raise CompletionError("--domain and --argv are required")
            passed, detail = run_command(args.argv, Path(args.cwd).expanduser().resolve())
            evidence = [f"command={' '.join(args.argv)[:500]}", f"exit_ok={passed}", detail]
            blocker = f"required {args.domain} command failed"
            next_action = "repair the failing check and rerun the completion harness"
            if not passed:
                register_failure(store, args.goal_id, args.domain, blocker=blocker, evidence=evidence, next_action=next_action)
            else:
                resolve_completion_tasks(args.domain, evidence, goal_id=args.goal_id)
            output = store.record_check(args.goal_id, args.domain, passed=passed, evidence=evidence, blocker=blocker, next_action=next_action)
        else:
            if not args.repo:
                raise CompletionError("--repo is required")
            all_ok = True
            evidence: list[str] = []
            for value in args.repo:
                passed, current = check_clean_repo(Path(value).expanduser().resolve(), allow=args.allow)
                all_ok = all_ok and passed
                evidence.extend([f"repo={value}:{item}" for item in current])
            if not all_ok:
                register_failure(store, args.goal_id, "canonical_parity", blocker="repository has unresolved changes", evidence=evidence, next_action="finish or explicitly resolve every repository change, then rerun parity checks")
            else:
                resolve_completion_tasks("canonical_parity", evidence, goal_id=args.goal_id)
            output = store.record_check(args.goal_id, "canonical_parity", passed=all_ok, evidence=evidence, blocker="repository has unresolved changes", next_action="finish or explicitly resolve every repository change, then rerun parity checks")
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        return 0
    except CompletionError as exc:
        print(json.dumps({"schema": SCHEMA, "status": "open", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
