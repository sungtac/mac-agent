#!/usr/bin/env python3
"""Read-only audit and migration-plan helper for Edge Agent credentials.

The module records paths, existence, and permission metadata only. It never
opens credential files and never copies, deletes, or rotates secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import stat
import subprocess
from pathlib import Path
from typing import Any


KNOWN_CREDENTIALS = {
    "calendar_client": (Path("calendar/google_calendar_client.json"), Path(".openclaw/secrets/google_calendar_client.json")),
    "calendar_token": (Path("calendar/google_calendar_token.json"), Path(".openclaw/secrets/google_calendar_token.json")),
    "telegram_claude": (Path("telegram/claude.token"), Path(".config/agent-telegram/claude.token")),
    "telegram_codex": (Path("telegram/codex.token"), Path(".config/agent-telegram/codex.token")),
    "telegram_antigravity": (Path("telegram/antigravity.token"), Path(".config/agent-telegram/antigravity.token")),
    "roda_telegram": (Path("roda-gemma/telegram.token"), Path(".config/roda-gemma/telegram.token")),
}

LAUNCH_AGENT_EXPECTATIONS = {
    "com.macagent.telegram-claude.plist": ("TELEGRAM_AGENT_TOKEN_FILE", Path("telegram/claude.token")),
    "com.macagent.telegram-codex.plist": ("TELEGRAM_AGENT_TOKEN_FILE", Path("telegram/codex.token")),
    "com.multiagent.engine.plist": ("TELEGRAM_BOT_TOKEN_FILE", Path("telegram/codex.token")),
    "com.macagent.telegram-antigravity.plist": ("TELEGRAM_AGENT_TOKEN_FILE", Path("telegram/antigravity.token")),
    "com.macagent.telegram-roda-gemma.plist": ("RODA_GEMMA_TOKEN_FILE", Path("roda-gemma/telegram.token")),
    "com.macagent.telegram-roda-health.plist": ("RODA_GEMMA_TOKEN_FILE", Path("roda-gemma/telegram.token")),
    "com.macagent.code-review-webhook-server.plist": ("CODE_REVIEW_WEBHOOK_SECRET_FILE", Path("code-review-webhook.secret")),
}

# A Telegram token must have exactly one long-polling consumer. The engine is
# the canonical Codex owner; the direct mac-agent Codex plist remains a
# disabled compatibility entry until feature parity is verified. Any other
# plist that points at one of these token paths is a runtime routing defect.
CANONICAL_LAUNCH_AGENT_LABELS = frozenset(LAUNCH_AGENT_EXPECTATIONS)


def _metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return {"path": str(path), "exists": True, "metadata": "unavailable"}
    return {
        "path": str(path),
        "exists": True,
        "mode": f"{mode:04o}",
        "safe_mode": mode & 0o077 == 0,
        "symlink": path.is_symlink(),
    }


def _read_launch_env(path: Path, key: str, expected_path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"plist": str(path), "exists": False, "key": key, "expected_path": str(expected_path)}
    try:
        payload = plistlib.loads(path.read_bytes())
        value = ((payload.get("EnvironmentVariables") or {}).get(key) if isinstance(payload, dict) else None)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
        return {"plist": str(path), "exists": True, "key": key, "expected_path": str(expected_path), "parseable": False}
    return {
        "plist": str(path),
        "exists": True,
        "key": key,
        "expected_path": str(expected_path),
        "configured_path": str(value) if value else "",
        "path_matches": str(value) == str(expected_path),
    }


def _read_loaded_launch_env(
    label: str,
    key: str,
    *,
    uid: int | None = None,
    runner=subprocess.run,
) -> dict[str, Any]:
    """Read only the selected loaded launchd environment value.

    launchd keeps an in-memory job definition.  This deliberately does not
    inspect the process environment or any credential file; it only asks
    launchctl for the selected key so a disk-vs-loaded drift can be reported.
    """
    domain = f"gui/{os.getuid() if uid is None else uid}/{label}"
    try:
        result = runner(
            ["/bin/launchctl", "print", domain],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return {"loaded": False, "error": type(exc).__name__}
    if result.returncode != 0:
        return {"loaded": False, "error": "not_loaded"}
    match = re.search(rf"^\s*{re.escape(key)}\s*=>\s*(.*?)\s*$", result.stdout, re.MULTILINE)
    if not match:
        return {"loaded": True, "configured_path": "", "error": "key_not_loaded"}
    return {"loaded": True, "configured_path": match.group(1)}


def _find_duplicate_token_consumers(agents: Path, canonical_root: Path) -> list[dict[str, str]]:
    """Find non-canonical LaunchAgents reusing an Edge Agent token path.

    This reads plist metadata only.  Credential contents are never opened.
    """
    canonical_paths = {
        str((canonical_root / expected_rel).expanduser().resolve())
        for _key, expected_rel in LAUNCH_AGENT_EXPECTATIONS.values()
    }
    duplicates: list[dict[str, str]] = []
    try:
        plist_paths = sorted(agents.glob("*.plist"))
    except OSError:
        return duplicates
    for plist_path in plist_paths:
        label = plist_path.name
        if label in CANONICAL_LAUNCH_AGENT_LABELS:
            continue
        try:
            payload = plistlib.loads(plist_path.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
            continue
        # A disk plist with Disabled=true is intentionally not an active
        # polling consumer.  Loaded launchd drift is checked separately by
        # the caller, so a retired plist does not keep the audit red forever.
        if isinstance(payload, dict) and payload.get("Disabled") is True:
            continue
        environment = payload.get("EnvironmentVariables") if isinstance(payload, dict) else None
        if not isinstance(environment, dict):
            continue
        for key, value in environment.items():
            try:
                normalized_value = str(Path(str(value)).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                normalized_value = str(value)
            if normalized_value in canonical_paths:
                duplicates.append({
                    "plist": str(plist_path),
                    "environment_key": str(key),
                    "token_path": str(value),
                })
    return duplicates


def audit(
    *,
    home: str | Path | None = None,
    secrets_root: str | Path | None = None,
    launch_agents_dir: str | Path | None = None,
    launchctl_runner=subprocess.run,
    launchctl_uid: int | None = None,
) -> dict[str, Any]:
    home_path = Path(home or Path.home()).expanduser().resolve()
    root = Path(secrets_root or home_path / ".edge-agent" / "secrets").expanduser().resolve()
    agents = Path(launch_agents_dir or home_path / "Library" / "LaunchAgents").expanduser().resolve()
    credentials = {}
    for name, (canonical_rel, legacy_rel) in KNOWN_CREDENTIALS.items():
        canonical = root / canonical_rel
        legacy = home_path / legacy_rel
        canonical_meta = _metadata(canonical)
        legacy_meta = _metadata(legacy)
        if legacy_meta["exists"] and not canonical_meta["exists"]:
            status = "needs_migration"
        elif not canonical_meta["exists"]:
            status = "missing"
        elif legacy_meta["exists"]:
            status = "duplicate_legacy"
        elif not canonical_meta.get("safe_mode", False) or canonical_meta.get("symlink", False):
            status = "unsafe_permissions"
        else:
            status = "ready"
        credentials[name] = {"status": status, "canonical": canonical_meta, "legacy": legacy_meta}

    code_review_path = root / "code-review-webhook.secret"
    code_review_meta = _metadata(code_review_path)
    if not code_review_meta["exists"]:
        code_review_status = "missing"
    elif not code_review_meta.get("safe_mode", False) or code_review_meta.get("symlink", False):
        code_review_status = "unsafe_permissions"
    else:
        code_review_status = "ready"
    credentials["code_review_webhook"] = {
        "status": code_review_status,
        "canonical": code_review_meta,
        "legacy": {"exists": False},
    }

    launch_agents = {}
    for name, (key, expected_rel) in LAUNCH_AGENT_EXPECTATIONS.items():
        disk = _read_launch_env(agents / name, key, root / expected_rel)
        loaded = _read_loaded_launch_env(
            name.removesuffix(".plist"),
            key,
            uid=launchctl_uid,
            runner=launchctl_runner,
        )
        loaded_path = loaded.get("configured_path", "")
        disk_path = disk.get("configured_path", "")
        if loaded.get("loaded") and disk_path:
            loaded["path_matches_disk"] = loaded_path == disk_path
        launch_agents[name] = {**disk, "loaded": loaded}
    duplicate_token_consumers = _find_duplicate_token_consumers(agents, root)
    configured_legacy = []
    for name, record in launch_agents.items():
        configured = record.get("configured_path", "")
        if record.get("exists") and not record.get("path_matches", False):
            configured_legacy.append(name)
    unknown_legacy = _metadata(home_path / ".openclaw" / "secrets" / "openclaw_env")
    blocking = [name for name, record in credentials.items() if record["status"] in {"needs_migration", "missing", "duplicate_legacy", "unsafe_permissions"}]
    if configured_legacy:
        blocking.extend(f"launchd:{name}" for name in configured_legacy)
    loaded_drift = [
        name
        for name, record in launch_agents.items()
        if record.get("loaded", {}).get("loaded")
        and record.get("loaded", {}).get("path_matches_disk") is False
    ]
    if loaded_drift:
        blocking.extend(f"launchd_loaded_drift:{name}" for name in loaded_drift)
    for duplicate in duplicate_token_consumers:
        blocking.append(f"launchd_duplicate_token_consumer:{Path(duplicate['plist']).name}")
    if unknown_legacy["exists"]:
        blocking.append("legacy:openclaw_env_manual_review")
    return {
        "schema": "edge_agent.auth_boundary_audit.v1",
        "mode": "read_only",
        "canonical_secrets_root": str(root),
        "credentials": credentials,
        "launch_agents": launch_agents,
        "duplicate_token_consumers": duplicate_token_consumers,
        "blocking_items": blocking,
        "ready": not blocking,
        "notes": [
            "Credential contents were not read.",
            "Legacy files are not copied or deleted by this audit.",
            "Loaded launchd values are read for drift detection; services are not reloaded by this audit.",
            "openclaw_env requires manual ownership and retention review because it has no one-to-one Edge Agent mapping.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit Edge Agent credential boundary without reading secrets")
    parser.add_argument("--home", default="")
    parser.add_argument("--secrets-root", default="")
    parser.add_argument("--launch-agents-dir", default="")
    parser.add_argument("--json", action="store_true", help="emit the JSON report (default)")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    report = audit(home=args.home or None, secrets_root=args.secrets_root or None, launch_agents_dir=args.launch_agents_dir or None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and not report["ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
