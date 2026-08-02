"""Validated catalog for repository-owned Edge Agent skills."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / "skills"
DEFAULT_CATALOG = SKILLS_ROOT / "catalog.json"
VALID_STATUSES = {"active", "merged", "legacy", "incomplete"}


def load_catalog(path: str | Path = DEFAULT_CATALOG) -> dict[str, Any]:
    catalog_path = Path(path).expanduser().resolve()
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    if payload.get("schema") != "edge_agent_skill_catalog.v1":
        raise ValueError("unsupported skill catalog schema")
    entries = payload.get("skills")
    if not isinstance(entries, list):
        raise ValueError("skill catalog skills must be a list")

    seen_ids: set[str] = set()
    seen_manifests: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("skill catalog entry must be an object")
        skill_id = entry.get("id")
        manifest = entry.get("manifest")
        status = entry.get("status")
        if not isinstance(skill_id, str) or not skill_id:
            raise ValueError("skill catalog entry has no id")
        if skill_id in seen_ids:
            raise ValueError(f"duplicate skill id: {skill_id}")
        if not isinstance(manifest, str) or not manifest or Path(manifest).is_absolute():
            raise ValueError(f"invalid manifest path for {skill_id}")
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status for {skill_id}: {status}")
        manifest_path = (SKILLS_ROOT / manifest).resolve()
        if SKILLS_ROOT.resolve() not in manifest_path.parents:
            raise ValueError(f"manifest escapes skills root: {skill_id}")
        if not manifest_path.is_file():
            raise ValueError(f"manifest is missing for {skill_id}: {manifest}")
        if manifest in seen_manifests:
            raise ValueError(f"duplicate skill manifest: {manifest}")
        seen_ids.add(skill_id)
        seen_manifests.add(manifest)
    return payload


def catalog_skill_ids(*, active_only: bool = True, path: str | Path = DEFAULT_CATALOG) -> tuple[str, ...]:
    entries = load_catalog(path)["skills"]
    if active_only:
        entries = [entry for entry in entries if entry["status"] == "active"]
    return tuple(sorted(entry["id"] for entry in entries))


def validate_catalog_covers_manifests(path: str | Path = DEFAULT_CATALOG) -> tuple[str, ...]:
    catalog = load_catalog(path)
    listed = {entry["manifest"] for entry in catalog["skills"]}
    actual = {str(item.relative_to(SKILLS_ROOT)) for item in SKILLS_ROOT.glob("*/SKILL.md")}
    return tuple(sorted(actual - listed))
