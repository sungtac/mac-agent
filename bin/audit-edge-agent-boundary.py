#!/usr/bin/env python3
"""Read-only audit of the Edge Agent / Team OS workspace boundary.

This tool inventories launchd plist metadata, active provider processes, shared
workspace overlap, git worktrees, Team OS git dirtiness, and nano-event state.
It never restarts services, writes files, changes permissions, or invokes an
agent/provider. Sensitive environment values are deliberately excluded.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable


DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "config" / "edge-agent-boundary.json"
SAFE_ENV_KEYS = {
    "TELEGRAM_AGENT_ROLE",
    "TELEGRAM_AGENT_WORKSPACE",
    "RODA_GEMMA_MODEL",
    "RODA_GEMMA_USERNAME",
}
PROTECTED_SERVICE_LABELS = {
    "com.macagent.telegram-claude",
    "com.macagent.telegram-codex",
    "com.macagent.telegram-antigravity",
    "com.multiagent.engine",
}


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    service: str = ""


@dataclass
class AuditReport:
    schema: str = "edge_agent_workspace_boundary_audit.v1"
    mode: str = "audit_only"
    findings: list[Finding] = field(default_factory=list)
    services: list[dict] = field(default_factory=list)
    processes: list[dict] = field(default_factory=list)
    worktrees: list[dict] = field(default_factory=list)
    team_os_git_status: dict = field(default_factory=dict)
    nano_event_store: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["findings"] = [asdict(item) for item in self.findings]
        return payload


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout


def _read_plist(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            value = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_env(environment: object) -> dict:
    if not isinstance(environment, dict):
        return {}
    return {key: str(environment[key]) for key in SAFE_ENV_KEYS if key in environment}


def _service_record(label: str, plist_path: Path, protected_roots: list[Path], legacy_workspace: Path) -> tuple[dict, list[Finding]]:
    findings: list[Finding] = []
    record = {"label": label, "plist_exists": plist_path.is_file()}
    if not plist_path.is_file():
        findings.append(Finding("warning", "launchagent_missing", "LaunchAgent plist not found", label))
        return record, findings

    plist = _read_plist(plist_path)
    args = plist.get("ProgramArguments")
    record["program"] = str(args[0]) if isinstance(args, list) and args else ""
    record["script"] = str(args[1]) if isinstance(args, list) and len(args) > 1 else ""
    record["working_directory"] = str(plist.get("WorkingDirectory", ""))
    record["safe_environment"] = _safe_env(plist.get("EnvironmentVariables"))
    record["keep_alive"] = bool(plist.get("KeepAlive"))
    record["run_at_load"] = bool(plist.get("RunAtLoad"))

    cwd = _resolve(record["working_directory"]) if record["working_directory"] else None
    if label in PROTECTED_SERVICE_LABELS and cwd and _is_within(cwd, legacy_workspace):
        findings.append(Finding(
            "high",
            "shared_workspace_overlap",
            "provider service working directory is inside the legacy shared workspace",
            label,
        ))
        for root in protected_roots:
            if _is_within(cwd, root):
                findings.append(Finding("high", "protected_root_overlap", f"service working directory overlaps protected root {root.name}", label))
    if label == "telegram_roda_gemma" and cwd and _is_within(cwd, legacy_workspace):
        findings.append(Finding("high", "roda_workspace_overlap", "Roda Gemma working directory overlaps legacy shared workspace", label))
    return record, findings


def _process_records(process_lines: Iterable[str], scripts: set[str]) -> list[dict]:
    records: list[dict] = []
    for line in process_lines:
        fields = line.strip().split(None, 2)
        if len(fields) < 3:
            continue
        pid, ppid, command = fields
        matched = next((script for script in scripts if script in command), None)
        if not matched:
            continue
        records.append({"pid": pid, "ppid": ppid, "script": matched})
    return records


def _worktree_records(text: str) -> list[dict]:
    records: list[dict] = []
    current: dict = {}
    for line in text.splitlines():
        if line.startswith("worktree "):
            if current:
                records.append(current)
            current = {"path": line[9:].strip()}
        elif line.startswith("branch "):
            current["branch"] = line[7:].strip()
    if current:
        records.append(current)
    return records


def _status_summary(status_text: str) -> dict:
    summary = {"modified": 0, "added": 0, "deleted": 0, "renamed": 0, "untracked": 0, "total": 0}
    for line in status_text.splitlines():
        if not line.strip():
            continue
        summary["total"] += 1
        if line.startswith("??"):
            summary["untracked"] += 1
        elif "R" in line[:2]:
            summary["renamed"] += 1
        elif "D" in line[:2]:
            summary["deleted"] += 1
        elif "A" in line[:2]:
            summary["added"] += 1
        else:
            summary["modified"] += 1
    return summary


def audit_boundary(
    manifest_path: str | Path = DEFAULT_MANIFEST,
    *,
    launch_agents_dir: str | Path | None = None,
    process_lines: Iterable[str] | None = None,
    worktree_text: str | None = None,
    team_status_text: str | None = None,
) -> AuditReport:
    manifest_file = _resolve(manifest_path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    report = AuditReport(mode=str(manifest.get("mode", "unknown")))
    legacy_workspace = _resolve(manifest["legacy_shared_workspace"])
    protected_roots = [_resolve(item) for item in manifest.get("protected_roots", [])]
    agents_dir = _resolve(launch_agents_dir or Path.home() / "Library" / "LaunchAgents")

    for role, label in manifest.get("runtime_services", {}).items():
        record, findings = _service_record(label, agents_dir / f"{label}.plist", protected_roots, legacy_workspace)
        record["role"] = role
        report.services.append(record)
        report.findings.extend(findings)

    if process_lines is None:
        process_lines = _run(["ps", "-axo", "pid=,ppid=,command="]).splitlines()
    scripts = {"telegram-agent-bot.py", "roda-gemma-bot.py", "adapter.py"}
    report.processes = _process_records(process_lines, scripts)
    expected_scripts = {"telegram-agent-bot.py", "roda-gemma-bot.py", "adapter.py"}
    observed_scripts = {item["script"] for item in report.processes}
    for script in sorted(expected_scripts - observed_scripts):
        report.findings.append(Finding("warning", "process_not_observed", "expected provider script was not observed in process snapshot", script))

    mac_agent = _resolve(manifest["edge_agent_source_root"])
    if worktree_text is None:
        worktree_text = _run(["git", "worktree", "list", "--porcelain"], cwd=mac_agent)
    report.worktrees = _worktree_records(worktree_text)
    if len(report.worktrees) > 1:
        ownership_path = _resolve(manifest.get("worktree_ownership_manifest", ""))
        if not ownership_path.is_file():
            report.findings.append(Finding("medium", "worktrees_present", "additional git worktrees exist; ownership and cleanup must be documented"))

    team_root = _resolve(manifest["legacy_shared_workspace"])
    if team_status_text is None:
        team_status_text = _run(["git", "status", "--porcelain"], cwd=team_root)
    report.team_os_git_status = _status_summary(team_status_text)
    legacy_policy = manifest.get("legacy_workspace_policy", {})
    report.team_os_git_status["policy"] = legacy_policy
    if report.team_os_git_status["total"] and legacy_policy.get("mode") != "quarantined_preserve_uncommitted":
        report.findings.append(Finding("high", "team_workspace_dirty", "legacy shared workspace has uncommitted changes"))

    nano_path = _resolve(Path.home() / ".claude" / "nano-gate-events.jsonl")
    report.nano_event_store = {
        "path": str(nano_path),
        "exists": nano_path.is_file(),
        "size_bytes": nano_path.stat().st_size if nano_path.is_file() else 0,
    }
    if not report.nano_event_store["exists"]:
        report.findings.append(Finding("medium", "nano_event_store_missing", "default nano event store does not exist"))

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Edge Agent workspace boundary audit")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--launch-agents-dir", default=None)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--strict", action="store_true", help="return 1 when high findings exist")
    args = parser.parse_args()
    try:
        report = audit_boundary(args.manifest, launch_agents_dir=args.launch_agents_dir)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema": "edge_agent_workspace_boundary_audit.v1", "error": str(exc)}, ensure_ascii=False))
        return 2
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"mode: {report.mode}")
        print(f"services: {len(report.services)} processes: {len(report.processes)} worktrees: {len(report.worktrees)}")
        for finding in report.findings:
            suffix = f" [{finding.service}]" if finding.service else ""
            print(f"{finding.severity.upper()} {finding.code}{suffix}: {finding.message}")
    if args.strict and any(item.severity == "high" for item in report.findings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
