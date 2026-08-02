"""Small, read-only connector from provider prompts to portable skill contracts."""

from __future__ import annotations

from pathlib import Path

from edge_agent_capability_registry import resolve, select_skill_ids as select_capability_skill_ids


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
def select_skill_ids(prompt: str) -> list[str]:
    return list(select_capability_skill_ids(prompt))


def build_skill_context(prompt: str, *, max_chars: int = 6000) -> str:
    # Keep a small budget for peer state on normal prompts. Very tight
    # contexts stay skill-only so required contracts are not displaced.
    peer_budget = 1200 if max_chars >= 2400 else 0
    context = resolve(prompt, max_chars=max_chars - peer_budget).context
    if peer_budget == 0:
        return context[:max_chars]
    try:
        from edge_agent_peer_context import snapshot
        peer_context = snapshot(max_chars=peer_budget)
    except (ImportError, OSError, ValueError, TypeError):
        peer_context = "[Peer coordination snapshot: unknown; peer state was not confirmed]"
    separator = "\n\n"
    available = max_chars - len(context) - len(separator)
    # Skill contracts keep their full priority under tight budgets. Peer
    # state is useful coordination metadata, but it must never evict the
    # selected skill (especially quota_resume) from a bounded prompt.
    if available < 200:
        return context[:max_chars]
    return context + separator + peer_context[:available]
