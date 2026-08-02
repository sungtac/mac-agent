#!/usr/bin/env python3
"""Thin, bounded skill-selection policy shared by provider adapters."""

from __future__ import annotations

from dataclasses import dataclass

from edge_agent_capability_registry import resolve, select_policy_skill_ids


@dataclass(frozen=True)
class SkillSelection:
    skill_ids: tuple[str, ...]
    context: str
    omitted: tuple[str, ...]


def select_skill_ids(prompt: str) -> tuple[str, ...]:
    return select_policy_skill_ids(prompt)


def build_skill_context(prompt: str, documents: dict[str, str], *, max_chars: int = 6000) -> SkillSelection:
    if max_chars < 100:
        raise ValueError("max_chars must be at least 100")
    selected = select_skill_ids(prompt)
    sections: list[str] = []
    omitted: list[str] = []
    remaining = max_chars
    for skill in selected:
        document = documents.get(skill)
        if not document:
            omitted.append(skill)
            continue
        block = f"\n[Edge Agent skill: {skill}]\n{document.strip()}\n"
        if len(block) > remaining:
            omitted.append(skill)
            continue
        sections.append(block)
        remaining -= len(block)
    common = resolve(prompt, max_chars=max(0, remaining), include_core=True) if remaining >= 100 else None
    if common is not None and common.context:
        sections.append(common.context[:remaining])
        omitted.extend(common.omitted)
    return SkillSelection(selected, "".join(sections)[:max_chars], tuple(dict.fromkeys(omitted)))
