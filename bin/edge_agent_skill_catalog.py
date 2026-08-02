"""Validated catalog for repository-owned Edge Agent skills."""

from __future__ import annotations

import json
import re
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
        manifest_text = manifest_path.read_text(encoding="utf-8")
        name_match = re.search(r"^name:\s*(\S+)\s*$", manifest_text, re.MULTILINE)
        if not name_match:
            raise ValueError(f"manifest has no frontmatter name for {skill_id}")
        normalize_id = lambda value: value.replace("-", "_")
        if normalize_id(name_match.group(1)) != normalize_id(skill_id):
            raise ValueError(f"catalog id does not match manifest name for {skill_id}")
        if manifest in seen_manifests:
            raise ValueError(f"duplicate skill manifest: {manifest}")
        tests = entry.get("tests", [])
        if not isinstance(tests, list) or any(
            not isinstance(test_path, str) or not test_path or Path(test_path).is_absolute() for test_path in tests
        ):
            raise ValueError(f"invalid tests paths for {skill_id}")
        for test_path in tests:
            repo_candidate = (ROOT / test_path).resolve()
            skills_candidate = (SKILLS_ROOT / test_path).resolve()
            repo_safe = ROOT.resolve() in repo_candidate.parents or repo_candidate == ROOT.resolve()
            skills_safe = SKILLS_ROOT.resolve() in skills_candidate.parents or skills_candidate == SKILLS_ROOT.resolve()
            if not (repo_safe and repo_candidate.exists()) and not (skills_safe and skills_candidate.exists()):
                if not repo_safe and not skills_safe:
                    raise ValueError(f"test path escapes repository root: {skill_id}")
                raise ValueError(f"test path is missing for {skill_id}: {test_path}")
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
