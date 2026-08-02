#!/usr/bin/env python3
"""Single skill/capability resolver shared by all Edge Agent adapters."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from edge_agent_capability_preflight import render_prompt
from edge_agent_skill_catalog import catalog_skill_ids, load_catalog


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
CORE_SKILL = "edge-agent-behavior"
SKILL_TRIGGERS = {
    "quota_resume": (
        "quota", "rate_limit", "rate limit", "429", "insufficient_quota", "토큰 한도", "재개", "fallback",
    ),
    "product_research": (
        "추천제품", "제품 추천", "최저가", "가성비", "쿠폰", "가격비교", "구매 링크",
        "product recommendation", "recommend a product", "lowest price", "price comparison", "buying link",
    ),
    "calendar": ("일정", "캘린더", "calendar", "schedule", "meeting", "appointment", "회의 일정", "약속"),
    "roda-public-search": (
        "공개자료", "공개 검색", "공개 출처", "논문 찾아", "사이트 찾아", "public source", "public sources",
        "web search", "find sources", "find a paper",
    ),
    "harness-memory": (
        "디버그", "디버깅", "반복 오류", "재현", "실패 원인", "debug", "debugging", "troubleshoot",
        "test failure", "regression", "retry failure",
    ),
    "hermes_runtime": (
        "반복 장애", "회귀", "퇴역", "live_verified", "증거 티켓", "hermes", "lifecycle", "retirement evidence",
    ),
    "code-review": (
        "코드리뷰", "코드 리뷰", "코드 점검", "코드점검", "코드 품질", "코드품질", "code review",
        "review this code", "review this diff", "pull request review", "quality review",
    ),
}

# These bounded policy signals are not portable skills in skills/catalog.json.
# They are nevertheless resolved here so every adapter uses one trigger owner.
POLICY_SKILL_TRIGGERS = {
    "context_budget": ("context", "compact", "clear", "handoff", "컨텍스트", "요약"),
    "minimality_review": ("minimal", "over-engineer", "ponytail", "간결", "불필요한 코드"),
    "quota_routing": ("quota", "rate limit", "429", "사용량", "토큰 한도"),
    "verification": ("test", "verify", "검증", "테스트", "diff"),
}


@dataclass(frozen=True)
class CapabilityResolution:
    capability_ids: tuple[str, ...]
    context: str
    omitted: tuple[str, ...]


def discover_skill_ids() -> tuple[str, ...]:
    return catalog_skill_ids()


def select_skill_ids(prompt: str) -> tuple[str, ...]:
    lowered = (prompt or "").casefold()
    return tuple(skill for skill, triggers in SKILL_TRIGGERS.items() if any(trigger.casefold() in lowered for trigger in triggers))


def select_policy_skill_ids(prompt: str) -> tuple[str, ...]:
    lowered = (prompt or "").casefold()
    return tuple(
        skill
        for skill, triggers in POLICY_SKILL_TRIGGERS.items()
        if any(trigger.casefold() in lowered for trigger in triggers)
    )


def resolve(prompt: str, *, max_chars: int = 6000, include_core: bool = True) -> CapabilityResolution:
    if max_chars < 100:
        raise ValueError("max_chars must be at least 100")
    catalog = load_catalog()
    manifest_paths = {
        entry["id"]: SKILLS_ROOT / entry["manifest"]
        for entry in catalog["skills"]
        if entry["status"] == "active"
    }
    discovered = set(manifest_paths)
    selected: list[str] = []
    if include_core and CORE_SKILL in discovered:
        selected.append(CORE_SKILL)
    for skill in select_skill_ids(prompt):
        if skill in discovered and skill not in selected:
            selected.append(skill)
    sections: list[str] = []
    omitted: list[str] = []
    remaining = max_chars
    for skill in selected:
        path = manifest_paths[skill]
        text = path.read_text(encoding="utf-8")
        block = f"\n[Edge Agent skill: {skill}]\n{text.strip()}\n"
        if remaining <= 0:
            omitted.append(skill)
            continue
        # Keep the old bounded behavior, but make truncation explicit and cut at
        # a line boundary. A provider must not mistake a partial contract for a
        # complete skill document.
        core_budget = 700 if max_chars < 2400 else 2200
        limit = min(remaining, core_budget) if skill == CORE_SKILL else remaining
        if len(block) > limit:
            excerpt_limit = max(0, limit - len("\n[truncated: incomplete skill contract]\n"))
            excerpt = block[:excerpt_limit].rsplit("\n", 1)[0]
            sections.append(excerpt + "\n[truncated: incomplete skill contract]\n")
            omitted.append(skill)
        else:
            sections.append(block)
        remaining -= min(len(block), limit)
    return CapabilityResolution(tuple(selected), "".join(sections), tuple(omitted))


def prepare_provider_argv(
    provider: str,
    args: list[str],
    *,
    max_chars: int = 6000,
    workdir: str | Path | None = None,
) -> list[str]:
    """Inject the common context into a known provider prompt argv slot.

    Only the prompt value is replaced; provider flags and cwd arguments are
    preserved byte-for-byte. Unknown argv shapes are returned unchanged.
    """
    prompt_index: int | None = None
    if provider == "claude" and "-p" in args:
        index = args.index("-p") + 1
        if index < len(args):
            prompt_index = index
    elif provider == "agy" and "--print" in args:
        index = args.index("--print") + 1
        if index < len(args):
            prompt_index = index
    elif provider == "codex" and "--" in args:
        index = len(args) - 1
        if index >= 0 and args[index] != "--":
            prompt_index = index
    if prompt_index is None:
        return list(args)
    original = args[prompt_index]
    preflight = render_prompt(workdir)
    context = resolve(original, max_chars=max_chars).context
    if not context:
        context = ""
    prepared = list(args)
    blocks = [block for block in (preflight, context, "[공통 가능 기능 요청]") if block]
    envelope = "\n\n".join(blocks)
    prepared[prompt_index] = f"{envelope}\n{original}"
    return prepared


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--max-chars", type=int, default=6000)
    args = parser.parse_args()
    print(resolve(args.prompt, max_chars=args.max_chars).context, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
