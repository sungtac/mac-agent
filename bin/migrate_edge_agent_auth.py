#!/usr/bin/env python3
"""Copy known credential files into Edge Agent secrets without deleting sources.

The default is a dry-run. Applying the copy requires an explicit confirmation
flag and still leaves legacy sources untouched for later verification.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from edge_agent_auth_boundary import KNOWN_CREDENTIALS


def _copy_secret(source: Path, destination: Path, *, allow_replace: bool = False) -> dict[str, Any]:
    if source.is_symlink():
        return {"status": "source_symlink_rejected", "source": str(source), "destination": str(destination)}
    if not source.is_file():
        return {"status": "source_not_regular_file", "source": str(source), "destination": str(destination)}
    if destination.exists() and not allow_replace:
        return {"status": "destination_exists", "source": str(source), "destination": str(destination)}
    if destination.is_symlink():
        return {"status": "destination_symlink_rejected", "source": str(source), "destination": str(destination)}
    source_mode = stat.S_IMODE(source.stat().st_mode)
    if source_mode & 0o077:
        return {"status": "source_unsafe_permissions", "source": str(source), "source_mode": f"{source_mode:04o}"}
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as source_handle, os.fdopen(fd, "wb") as destination_handle:
            shutil.copyfileobj(source_handle, destination_handle)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    destination_mode = stat.S_IMODE(destination.stat().st_mode)
    return {
        "status": "copied",
        "source": str(source),
        "destination": str(destination),
        "source_size": source.stat().st_size,
        "destination_size": destination.stat().st_size,
        "source_mode": f"{source_mode:04o}",
        "destination_mode": f"{destination_mode:04o}",
        "verified": source.stat().st_size == destination.stat().st_size and destination_mode & 0o077 == 0,
    }


def plan(*, home: str | Path | None = None, secrets_root: str | Path | None = None, selected: set[str] | None = None, apply: bool = False, confirm_copy: bool = False, allow_replace: bool = False) -> dict[str, Any]:
    home_path = Path(home or Path.home()).expanduser().resolve()
    root = Path(secrets_root or home_path / ".edge-agent" / "secrets").expanduser().resolve()
    names = sorted(selected or KNOWN_CREDENTIALS)
    records = []
    for name in names:
        if name not in KNOWN_CREDENTIALS:
            records.append({"name": name, "status": "unknown_credential_id"})
            continue
        canonical_rel, legacy_rel = KNOWN_CREDENTIALS[name]
        source = home_path / legacy_rel
        destination = root / canonical_rel
        record: dict[str, Any] = {"name": name, "source": str(source), "destination": str(destination)}
        if not source.is_file():
            record["status"] = "source_missing"
        elif not apply:
            record["status"] = "planned_copy"
        elif not confirm_copy:
            record["status"] = "confirmation_required"
        else:
            root.mkdir(parents=True, exist_ok=True)
            os.chmod(root, 0o700)
            record.update(_copy_secret(source, destination, allow_replace=allow_replace))
        records.append(record)
    return {
        "schema": "edge_agent.auth_migration_plan.v1",
        "mode": "apply_copy" if apply and confirm_copy else "dry_run",
        "source_files_retained": True,
        "records": records,
        "notes": [
            "Credential contents are never printed.",
            "This tool never deletes or quarantines legacy files.",
            "Run the boundary audit after any copy before changing LaunchAgents.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or explicitly copy known credentials into Edge Agent secrets")
    parser.add_argument("--home", default="")
    parser.add_argument("--secrets-root", default="")
    parser.add_argument("--credential", action="append", dest="credentials")
    parser.add_argument("--apply", action="store_true", help="perform the copy; requires --confirm-copy")
    parser.add_argument("--confirm-copy", action="store_true", help="explicitly confirm credential copying")
    parser.add_argument("--allow-replace", action="store_true", help="allow replacing an existing canonical file")
    args = parser.parse_args(argv)
    report = plan(
        home=args.home or None,
        secrets_root=args.secrets_root or None,
        selected=set(args.credentials) if args.credentials else None,
        apply=args.apply,
        confirm_copy=args.confirm_copy,
        allow_replace=args.allow_replace,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.apply and not args.confirm_copy:
        return 2
    return 0 if all(item.get("status") in {"planned_copy", "copied"} for item in report["records"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
