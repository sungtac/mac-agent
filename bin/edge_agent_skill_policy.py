#!/usr/bin/env python3
"""Thin, bounded skill-selection policy shared by provider adapters."""

from __future__ import annotations

from dataclasses import dataclass


SKILL_TRIGGERS = {
    "context_budget": ("context", "compact", "clear", "handoff", "컨텍스트", "요약"),
    "minimality_review": ("minimal", "over-engineer", "ponytail", "간결", "불필요한 코드"),
    "quota_routing": ("quota", "rate limit", "429", "사용량", "토큰 한도"),
    "verification": ("test", "verify", "검증", "테스트", "diff"),
}


@dataclass(frozen=True)
class SkillSelection:
    skill_ids: tuple[str, ...]
    context: str
    omitted: tuple[str, ...]


def select_skill_ids(prompt: str) -> tuple[str, ...]:
    lowered = (prompt or "").casefold()
    return tuple(skill for skill, triggers in SKILL_TRIGGERS.items() if any(trigger.casefold() in lowered for trigger in triggers))


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
    return SkillSelection(selected, "".join(sections), tuple(omitted))
