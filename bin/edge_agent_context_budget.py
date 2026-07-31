#!/usr/bin/env python3
"""Deterministic context and log budgets derived from the token-efficiency plan."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


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
    usable = max(0, limit - len(suffix))
    return (value[-usable:] if tail else value[:usable]).rstrip() + suffix


def bound_items(items: Iterable[str], limit: int, max_items: int) -> list[str]:
    if max_items < 0:
        raise ValueError("max_items must not be negative")
    values = [bound_text(item, limit) for item in items]
    return values[-max_items:] if max_items else []
