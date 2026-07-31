#!/usr/bin/env python3
"""Validate code-review service configuration without changing system state."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "code-review-repositories.json"
DEFAULT_PLIST = ROOT / "config" / "com.macagent.code-review-worker.plist.template"
DEFAULT_WEBHOOK_PLIST = ROOT / "config" / "com.macagent.code-review-webhook-server.plist.template"
CONFIG_SCHEMA = "edge_agent.code_review_repositories.v1"


def expand_path(value: str) -> Path:
    home = Path(os.environ.get("HOME", str(Path.home())))
    expanded = value.replace("${HOME}", str(home)).replace("$HOME", str(home))
    if expanded == "~" or expanded.startswith("~/"):
        expanded = str(home) + expanded[1:]
    return Path(expanded).expanduser().resolve()


def run_git(root: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def normalize_remote(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?$", value.strip())
    return match.group(1) if match else None


def provider_path(env_name: str, candidates: list[Path], command: str) -> str | None:
    override = os.environ.get(env_name)
    if override and Path(override).is_file() and os.access(override, os.X_OK):
        return override
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(command)


def preflight(config_path: Path = DEFAULT_CONFIG, plist_path: Path = DEFAULT_PLIST, *, webhook_plist_path: Path = DEFAULT_WEBHOOK_PLIST, require_clean: bool = False, require_providers: bool = False, allow_execute: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "errors": ["repository mapping config cannot be read"], "warnings": [], "checks": {}}
    if config.get("schema") != CONFIG_SCHEMA or not isinstance(config.get("repositories"), dict):
        return {"ok": False, "errors": ["repository mapping config schema is invalid"], "warnings": [], "checks": {}}

    repositories: dict[str, Any] = {}
    for name, entry in config["repositories"].items():
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", name) or not isinstance(entry, dict):
            errors.append(f"invalid repository mapping: {name}")
            continue
        try:
            root = expand_path(str(entry["repository_root"]))
        except (KeyError, TypeError, ValueError):
            errors.append(f"repository root missing: {name}")
            continue
        isolated = entry.get("isolated", True)
        record: dict[str, Any] = {"root": str(root), "enabled": entry.get("enabled", True), "isolated": isolated}
        if not root.is_dir():
            errors.append(f"repository root does not exist: {name}")
            repositories[name] = record
            continue
        top = run_git(root, "rev-parse", "--show-toplevel")
        if not top or Path(top).resolve() != root:
            errors.append(f"repository root is not a git worktree root: {name}")
            repositories[name] = record
            continue
        remote = normalize_remote(run_git(root, "remote", "get-url", "origin"))
        record["remote"] = remote
        if remote and remote != name:
            warnings.append(f"origin remote differs from mapping: {name}")
        elif not remote:
            warnings.append(f"origin remote unavailable: {name}")
        dirty = bool(run_git(root, "status", "--porcelain", "--untracked-files=all"))
        record["clean"] = not dirty
        if dirty:
            message = f"repository source worktree is dirty; isolated worker mode is required: {name}"
            if isolated and not require_clean:
                warnings.append(message)
            else:
                (errors if require_clean else warnings).append(message)
        repositories[name] = record
    checks["repositories"] = repositories

    try:
        plist = plistlib.loads(plist_path.read_bytes())
        args = plist.get("ProgramArguments", [])
        checks["plist_label"] = plist.get("Label")
        checks["plist_has_execute"] = "--execute" in args
        if plist.get("Label") != "com.macagent.code-review-worker": errors.append("launchd plist label is invalid")
        if "--execute" in args and not allow_execute: errors.append("launchd plist enables --execute; pass allow_execute explicitly")
        for path_value in (ROOT / "bin" / "code-review-worker-runner.js", DEFAULT_CONFIG):
            if not path_value.is_file(): errors.append(f"launchd dependency is missing: {path_value.name}")
    except (OSError, plistlib.InvalidFileException, ValueError):
        errors.append("launchd plist cannot be read")

    try:
        webhook_plist = plistlib.loads(webhook_plist_path.read_bytes())
        webhook_args = webhook_plist.get("ProgramArguments", [])
        checks["webhook_plist_label"] = webhook_plist.get("Label")
        checks["webhook_plist_has_secret_value"] = any("WEBHOOK_SECRET" in str(value) for value in webhook_args)
        if webhook_plist.get("Label") != "com.macagent.code-review-webhook-server": errors.append("webhook launchd plist label is invalid")
        if checks["webhook_plist_has_secret_value"]: errors.append("webhook launchd plist must not contain a secret value")
    except (OSError, plistlib.InvalidFileException, ValueError):
        errors.append("webhook launchd plist cannot be read")

    providers = {
        "codex": provider_path("CODEX_BIN", [Path.home() / ".local/bin/codex", Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex")], "codex"),
        "antigravity": provider_path("AGY_BIN", [Path.home() / ".local/bin/agy", Path("/opt/homebrew/bin/agy"), Path("/usr/local/bin/agy")], "agy"),
    }
    checks["providers_available"] = {name: bool(value) for name, value in providers.items()}
    for name, value in providers.items():
        if not value:
            (errors if require_providers else warnings).append(f"provider executable not found: {name}")

    return {"ok": not errors, "errors": errors, "warnings": warnings, "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--plist", type=Path, default=DEFAULT_PLIST)
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--require-providers", action="store_true")
    parser.add_argument("--allow-execute", action="store_true")
    args = parser.parse_args()
    result = preflight(args.config, args.plist, require_clean=args.require_clean, require_providers=args.require_providers, allow_execute=args.allow_execute)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
