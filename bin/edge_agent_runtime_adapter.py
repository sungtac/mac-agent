#!/usr/bin/env python3
"""Opt-in runtime adapter for the video-derived efficiency policies.

This module is the single integration boundary for future Telegram, Discord,
and terminal adapters. It prepares a bounded prompt and declarative provider
options, but never executes a provider, changes a workspace, or enables a
policy implicitly. ``EDGE_AGENT_EFFICIENCY_MODE`` defaults to ``off``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from edge_agent_context_budget import ContextBudget, bound_text, budget_for
from edge_agent_efficiency_events import EfficiencyEvent, EfficiencyStore
from edge_agent_execution_profile import ExecutionKind, ExecutionProfile, Provider, choose_profile, provider_args
from edge_agent_minimality import MinimalityMode, MinimalityReview, review_for
from edge_agent_skill_policy import SkillSelection, build_skill_context


class EfficiencyMode(StrEnum):
    OFF = "off"
    OBSERVE = "observe"
    ENFORCE = "enforce"


_CODING_WORDS = ("코딩", "코드", "파일", "구현", "수정", "버그", "개발", "coding", "implement", "refactor")
_RESEARCH_WORDS = ("조사", "리서치", "검색", "자료", "출처", "research", "compare")
_REVIEW_WORDS = ("검증", "리뷰", "감사", "테스트", "verify", "review", "audit")


def configured_mode(value: str | None = None) -> EfficiencyMode:
    selected = (value if value is not None else os.environ.get("EDGE_AGENT_EFFICIENCY_MODE", "off")).strip().lower()
    try:
        return EfficiencyMode(selected)
    except ValueError as exc:
        raise ValueError(f"unsupported EDGE_AGENT_EFFICIENCY_MODE: {selected!r}") from exc


def infer_kind(prompt: str) -> ExecutionKind:
    lowered = (prompt or "").casefold()
    if any(word.casefold() in lowered for word in _REVIEW_WORDS):
        return ExecutionKind.FULL_REVIEW
    if any(word.casefold() in lowered for word in _RESEARCH_WORDS):
        return ExecutionKind.RESEARCH
    if any(word.casefold() in lowered for word in _CODING_WORDS):
        return ExecutionKind.NANO_MID
    return ExecutionKind.CHAT


@dataclass(frozen=True)
class PreparedInvocation:
    original_prompt: str
    prompt: str
    provider: Provider
    kind: ExecutionKind
    profile: ExecutionProfile
    budget: ContextBudget
    minimality: MinimalityReview
    skills: SkillSelection
    mode: EfficiencyMode
    prepared_at: float

    @property
    def context_chars(self) -> int:
        return max(0, len(self.prompt) - len(self.original_prompt))

    def cli_options(self) -> dict[str, str | int]:
        """Return declarative options; the owning adapter maps supported flags."""
        return provider_args(self.profile)


class RuntimeEfficiencyAdapter:
    """Prepare and record an invocation without owning provider execution."""

    def __init__(
        self,
        *,
        mode: EfficiencyMode | str | None = None,
        event_store: EfficiencyStore | None = None,
    ) -> None:
        self.mode = configured_mode(mode.value if isinstance(mode, EfficiencyMode) else mode)
        self.events = event_store or EfficiencyStore()

    def prepare(
        self,
        prompt: str,
        *,
        provider: Provider | str,
        kind: ExecutionKind | str | None = None,
        context: str = "",
        skill_documents: Mapping[str, str] | None = None,
        minimality_mode: MinimalityMode | str = MinimalityMode.FULL,
        sensitive_path: bool = False,
    ) -> PreparedInvocation:
        original = str(prompt or "")
        selected_kind = ExecutionKind(kind) if kind is not None else infer_kind(original)
        selected_provider = Provider(provider)
        profile = choose_profile(selected_kind, provider=selected_provider)
        budget = budget_for(profile.context_profile)
        minimality = review_for(
            mode=minimality_mode,
            sensitive_path=sensitive_path,
            task_kind=selected_kind.value,
        )
        skills = build_skill_context(
            original,
            dict(skill_documents or {}),
            max_chars=min(6000, budget.max_context_chars),
        )

        if self.mode == EfficiencyMode.ENFORCE:
            bounded_prompt = bound_text(original, budget.max_context_chars)
            remaining = max(0, budget.max_context_chars - len(bounded_prompt))
            bounded_context = bound_text(context, remaining) if context and remaining else ""
            remaining -= len(bounded_context)
            skill_block = bound_text(skills.context, remaining) if skills.context and remaining else ""
            sections = [part for part in (bounded_context, skill_block, f"[사용자 요청]\n{bounded_prompt}") if part]
            # Section labels and separators consume budget too. The final
            # bound is required after composition; keep the tail so the
            # request itself survives if the envelope is exactly full.
            rendered = bound_text("\n\n".join(sections), budget.max_context_chars, tail=True)
        else:
            rendered = original

        return PreparedInvocation(
            original_prompt=original,
            prompt=rendered,
            provider=selected_provider,
            kind=selected_kind,
            profile=profile,
            budget=budget,
            minimality=minimality,
            skills=skills,
            mode=self.mode,
            prepared_at=time.monotonic(),
        )

    def record(
        self,
        prepared: PreparedInvocation,
        *,
        task_id: str,
        step_id: str,
        status: str,
        output: str = "",
        tool_turns: int = 0,
        changed_files: int = 0,
        duration_ms: int | None = None,
        verification_tier: str = "",
    ) -> str:
        elapsed = duration_ms if duration_ms is not None else max(0, int((time.monotonic() - prepared.prepared_at) * 1000))
        event = EfficiencyEvent(
            task_id=task_id,
            step_id=step_id,
            event_idempotency_key=f"{task_id}:{step_id}",
            provider=prepared.provider.value,
            profile=prepared.profile.kind.value,
            status=status,
            context_chars=prepared.context_chars,
            prompt_chars=len(prepared.prompt),
            output_chars=len(output or ""),
            tool_turns=tool_turns,
            changed_files=changed_files,
            duration_ms=elapsed,
            verification_tier=verification_tier,
        )
        return self.events.append(event)
