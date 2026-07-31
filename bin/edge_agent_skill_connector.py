"""Small, read-only connector from provider prompts to portable skill contracts."""

from __future__ import annotations

from pathlib import Path

from edge_agent_capability_registry import CORE_SKILL, SKILL_TRIGGERS, resolve


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
TRIGGERS = SKILL_TRIGGERS


def select_skill_ids(prompt: str) -> list[str]:
    lowered = prompt.casefold()
    return [skill for skill, triggers in TRIGGERS.items() if any(t.casefold() in lowered for t in triggers)]


def build_skill_context(prompt: str, *, max_chars: int = 6000) -> str:
    return resolve(prompt, max_chars=max_chars).context
