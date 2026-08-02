"""Fail-closed loader for skill repositories kept outside this repository."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "external-skill-repositories.json"


def load_external_skill_repositories(path: str | Path = DEFAULT_CONFIG) -> dict[str, Path]:
    config_path = Path(path).expanduser().resolve()
    payload: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
    if payload.get("schema") != "edge_agent_external_skill_repositories.v1":
        raise ValueError("unsupported external skill repository schema")
    repositories = payload.get("repositories")
    if not isinstance(repositories, dict) or not repositories:
        raise ValueError("external skill repositories must be a non-empty object")

    resolved: dict[str, Path] = {}
    for alias, entry in repositories.items():
        if not isinstance(alias, str) or not alias or not isinstance(entry, dict):
            raise ValueError("invalid external skill repository entry")
        if entry.get("status") != "external_dependency":
            raise ValueError(f"invalid external skill status for {alias}")
        raw_path = entry.get("path")
        manifest = entry.get("manifest")
        if not isinstance(raw_path, str) or not isinstance(manifest, str) or Path(manifest).is_absolute():
            raise ValueError(f"invalid external skill path for {alias}")
        candidate = Path(os.path.expandvars(raw_path)).expanduser()
        if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_dir():
            raise ValueError(f"external skill repository is unavailable or symlinked: {alias}")
        repository = candidate.resolve()
        manifest_path = (repository / manifest).resolve()
        if repository not in manifest_path.parents or not manifest_path.is_file():
            raise ValueError(f"external skill manifest is unavailable: {alias}")
        resolved[alias] = repository
    return resolved
