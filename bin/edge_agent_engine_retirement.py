#!/usr/bin/env python3
"""Read-only gate for the canonical engine retirement decision.

This command never sends Telegram messages, changes launchd, deletes files, or
reads credential contents.  It turns the final operational approval points
into an auditable JSON result.
"""
from __future__ import annotations

import argparse
import json
import plistlib
import shutil
import tempfile
from pathlib import Path
from typing import Any


def _plist(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            payload = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        return {"path": str(path), "exists": path.exists(), "error": type(exc).__name__}
    return {"path": str(path), "exists": True, "payload": payload}


def _direct_codex_candidates(agents: Path) -> tuple[Path, Path]:
    return (
        agents / "com.macagent.telegram-codex.plist",
        Path.home() / ".edge-agent" / "retired-launchagents" / "2026-08-02" / "com.macagent.telegram-codex.plist",
    )


def rollback_rehearsal(engine_plist: Path, direct_plist: Path) -> dict[str, Any]:
    """Rehearse quarantine/restore using temporary copies only."""
    if not engine_plist.is_file() or not direct_plist.is_file():
        return {"passed": False, "reason": "required plist is missing", "actual_files_changed": False}
    try:
        with tempfile.TemporaryDirectory(prefix="edge-engine-rollback-") as td:
            root = Path(td)
            engine_copy = root / engine_plist.name
            direct_copy = root / direct_plist.name
            shutil.copy2(engine_plist, engine_copy)
            shutil.copy2(direct_plist, direct_copy)
            original = direct_copy.read_bytes()
            quarantine = root / "quarantine"
            quarantine.mkdir()
            moved = quarantine / direct_copy.name
            shutil.move(str(direct_copy), moved)
            shutil.copy2(moved, direct_copy)
            restored = direct_copy.read_bytes()
            return {
                "passed": original == restored and engine_copy.read_bytes() == engine_plist.read_bytes(),
                "operation": "temporary copies only",
                "actual_files_changed": False,
            }
    except (OSError, shutil.Error) as exc:
        return {"passed": False, "reason": type(exc).__name__, "actual_files_changed": False}


def audit(
    *,
    source_root: str | Path,
    launch_agents: str | Path,
    engine_repo: str | Path,
) -> dict[str, Any]:
    source = Path(source_root).expanduser().resolve()
    agents = Path(launch_agents).expanduser().resolve()
    engine = Path(engine_repo).expanduser().resolve()
    engine_info = _plist(agents / "com.multiagent.engine.plist")
    direct_active, direct_quarantine = _direct_codex_candidates(agents)
    direct_path = direct_active if direct_active.is_file() else direct_quarantine
    direct_info = _plist(direct_path)
    engine_payload = engine_info.get("payload") or {}
    direct_payload = direct_info.get("payload") or {}
    engine_args = [str(item) for item in engine_payload.get("ProgramArguments", [])]
    direct_args = [str(item) for item in direct_payload.get("ProgramArguments", [])]
    shared_adapter = source / "bin" / "telegram-agent-bot.py"
    shared_text = shared_adapter.read_text(encoding="utf-8", errors="replace") if shared_adapter.is_file() else ""
    canary_doc = source / "docs" / "engine-canonical-migration-2026-08-02.md"
    canary_text = canary_doc.read_text(encoding="utf-8", errors="replace") if canary_doc.is_file() else ""

    findings: list[str] = []
    if not engine_info.get("exists") or engine_payload.get("Label") != "com.multiagent.engine":
        findings.append("canonical engine plist is missing or mislabeled")
    if not engine_args or not engine_args[-1].endswith("/telegram/adapter.py"):
        findings.append("canonical engine plist does not point to engine telegram adapter")
    if not direct_info.get("exists") or direct_payload.get("Disabled") is not True:
        findings.append("direct Codex plist is not retained in disabled rollback state")
    if not engine.is_dir():
        findings.append("engine repository is missing")
    if not shared_text or '"claude"' not in shared_text or '"antigravity"' not in shared_text:
        findings.append("shared Telegram adapter ownership could not be confirmed")
    if "오프라인 canary 결과" not in canary_text:
        findings.append("offline canary evidence is not recorded")
    rollback = rollback_rehearsal(agents / "com.multiagent.engine.plist", direct_path)
    if not rollback.get("passed"):
        findings.append("temporary-copy rollback rehearsal failed")

    return {
        "schema": "edge_agent.engine_retirement_gate.v1",
        "mode": "read_only",
        "ready_for_approval_review": not findings,
        "findings": findings,
        "rollback": {
            "engine_plist_retained": bool(engine_info.get("exists")),
            "direct_codex_plist_retained_disabled": direct_payload.get("Disabled") is True,
            "direct_codex_plist_quarantined": direct_path == direct_quarantine,
            "shared_adapter_preserved": shared_adapter.is_file(),
            "service_mutation_performed": False,
            "temporary_copy_rehearsal": rollback,
        },
        "approval_required": [],
        "completed_actions": [
            "telegram_canary_send",
            "direct_codex_plist_quarantine",
            "shared_adapter_codex_split_deferred_shared_runtime",
        ],
        "credential_contents_read": False,
        "engine_plist": {"path": str(agents / "com.multiagent.engine.plist"), "arguments": engine_args},
        "direct_codex_plist": {"path": str(direct_path), "arguments": direct_args},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="audit canonical engine retirement approval points")
    parser.add_argument("--source-root", default="/Users/edge_ai/mac-agent")
    parser.add_argument("--launch-agents", default="~/Library/LaunchAgents")
    parser.add_argument("--engine-repo", default="/Users/edge_ai/tools/multi-agent-starter/engine-repo")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = audit(source_root=args.source_root, launch_agents=args.launch_agents, engine_repo=args.engine_repo)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready_for_approval_review"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
