#!/usr/bin/env python3
"""Deterministic context and log budgets derived from the token-efficiency plan."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Sequence


class ContextProfile(StrEnum):
    CHAT = "chat"
    RESEARCH = "research"
    NANO_LIGHT = "nano_light"
    NANO_MID = "nano_mid"
    FULL_REVIEW = "full_review"


_SENSITIVE = ("token=", "api_key", "authorization:", "bearer ", "password=", "cookie:", "secret=")


@dataclass(frozen=True)
class ContextBudget:
    profile: ContextProfile
    max_context_chars: int
    max_summary_chars: int
    max_log_chars: int
    max_files: int
    max_decisions: int


POLICIES = {
    ContextProfile.CHAT: ContextBudget(ContextProfile.CHAT, 2400, 900, 700, 12, 6),
    ContextProfile.RESEARCH: ContextBudget(ContextProfile.RESEARCH, 5000, 1800, 1400, 30, 12),
    ContextProfile.NANO_LIGHT: ContextBudget(ContextProfile.NANO_LIGHT, 4000, 1400, 1100, 20, 10),
    ContextProfile.NANO_MID: ContextBudget(ContextProfile.NANO_MID, 6000, 2200, 1800, 40, 16),
    ContextProfile.FULL_REVIEW: ContextBudget(ContextProfile.FULL_REVIEW, 8000, 3000, 2400, 80, 24),
}


def budget_for(profile: ContextProfile | str) -> ContextBudget:
    return POLICIES[ContextProfile(profile)]


def estimate_input_tokens(text: str) -> int:
    """Conservative rough estimate for comparisons only; never a billing value."""
    return math.ceil(len(text or "") / 4)


def _safe(text: str) -> str:
    value = str(text or "")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE):
        raise ValueError("context contains a sensitive marker")
    return value


def bound_text(text: str, limit: int, *, tail: bool = False) -> str:
    value = _safe(text)
    if limit < 1:
        raise ValueError("limit must be positive")
    if len(value) <= limit:
        return value
    suffix = "\n…(축약)…"
    if limit <= len(suffix):
        return value[:limit]
    usable = max(0, limit - len(suffix))
    return (value[-usable:] if tail else value[:usable]).rstrip() + suffix


def bound_items(items: Iterable[str], limit: int, max_items: int) -> list[str]:
    if max_items < 0:
        raise ValueError("max_items must not be negative")
    values = [bound_text(item, limit) for item in items]
    return values[-max_items:] if max_items else []


def compress_sections(
    sections: Sequence[tuple[str, str, int]],
    limit: int,
) -> str:
    """Compress labelled context deterministically while preserving priority.

    ``priority`` is a relative weight, not a trust decision.  The caller must
    place user/task instructions at the highest priority and evidence or skill
    context below them.  Sensitive markers are rejected before truncation so a
    secret cannot be hidden by compression.
    """

    if limit < 1:
        raise ValueError("limit must be positive")
    normalized: list[tuple[int, str, str, int]] = []
    for index, (name, text, priority) in enumerate(sections):
        label = _safe(name).strip()
        value = _safe(text).strip()
        if not value:
            continue
        if not label or priority < 1:
            raise ValueError("section name and priority are required")
        normalized.append((index, label, value, int(priority)))
    if not normalized:
        return ""

    overhead = sum(len(f"[{name}]\n") for _, name, _, _ in normalized)
    overhead += max(0, len(normalized) - 1) * 2
    content_limit = max(1, limit - overhead)
    allocations = {index: 0 for index, _, _, _ in normalized}
    remaining = content_limit
    pending = list(normalized)
    while pending and remaining > 0:
        total_weight = sum(item[3] for item in pending)
        distributed = 0
        next_pending: list[tuple[int, str, str, int]] = []
        for index, name, value, priority in pending:
            share = max(1, (remaining * priority) // total_weight)
            amount = min(len(value), share)
            allocations[index] += amount
            remaining -= amount
            distributed += amount
            if allocations[index] < len(value):
                next_pending.append((index, name, value, priority))
        if distributed == 0:
            break
        pending = next_pending

    rendered: list[str] = []
    for index, name, value, _ in normalized:
        amount = allocations[index]
        if amount <= 0:
            continue
        rendered.append(f"[{name}]\n{bound_text(value, amount)}")
    return bound_text("\n\n".join(rendered), limit, tail=False)
