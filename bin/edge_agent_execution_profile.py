#!/usr/bin/env python3
"""Provider-neutral execution profiles for cost and reasoning control."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class Provider(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    ANTIGRAVITY = "antigravity"
    GEMMA = "gemma"


class ExecutionKind(StrEnum):
    CHAT = "chat"
    RESEARCH = "research"
    NANO_LIGHT = "nano_light"
    NANO_MID = "nano_mid"
    FULL_REVIEW = "full_review"


@dataclass(frozen=True)
class ExecutionProfile:
    kind: ExecutionKind
    provider: Provider
    model: str
    reasoning: str
    max_turns: int
    context_profile: str
    automatic_merge: bool = False

    def to_dict(self) -> dict:
        result = asdict(self)
        result["kind"] = self.kind.value
        result["provider"] = self.provider.value
        return result


_REASONING = {"minimal", "low", "medium", "high"}


def choose_profile(kind: ExecutionKind | str, *, provider: Provider | str) -> ExecutionProfile:
    task = ExecutionKind(kind)
    selected = Provider(provider)
    if task == ExecutionKind.CHAT:
        reasoning, turns, context = "low", 2, "chat"
    elif task == ExecutionKind.RESEARCH:
        reasoning, turns, context = "medium", 5, "research"
    elif task == ExecutionKind.NANO_LIGHT:
        reasoning, turns, context = "low", 4, "nano_light"
    elif task == ExecutionKind.NANO_MID:
        reasoning, turns, context = "medium", 8, "nano_mid"
    else:
        reasoning, turns, context = "high", 12, "full_review"
    model = "default"
    if selected == Provider.CLAUDE:
        model = "sonnet" if task != ExecutionKind.FULL_REVIEW else "opus"
    elif selected == Provider.CODEX:
        model = "default"
    elif selected in {Provider.ANTIGRAVITY, Provider.GEMMA}:
        model = "default"
    if reasoning not in _REASONING or turns < 1:
        raise ValueError("invalid execution profile")
    return ExecutionProfile(task, selected, model, reasoning, turns, context)


def provider_args(profile: ExecutionProfile) -> dict[str, str | int]:
    """Return declarative options; adapters decide which CLI flags are supported."""
    result: dict[str, str | int] = {"max_turns": profile.max_turns}
    if profile.provider == Provider.CLAUDE:
        result.update({"model": profile.model})
    elif profile.provider == Provider.CODEX:
        result.update({"reasoning_effort": profile.reasoning})
    elif profile.provider == Provider.ANTIGRAVITY:
        result.update({"mode": profile.reasoning})
    return result
