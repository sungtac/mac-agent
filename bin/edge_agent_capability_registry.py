#!/usr/bin/env python3
"""Single skill/capability resolver shared by all Edge Agent adapters."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
CORE_SKILL = "edge-agent-behavior"
SKILL_TRIGGERS = {
    "quota_resume": ("quota", "rate_limit", "429", "토큰 한도", "재개", "fallback"),
    "product_research": ("추천제품", "제품 추천", "최저가", "가성비", "쿠폰", "가격비교", "구매 링크"),
    "calendar": ("일정", "캘린더", "calendar", "회의 일정", "약속"),
    "roda_public_search": ("공개자료", "공개 검색", "출처", "논문 찾아", "사이트 찾아"),
    "harness-memory": ("디버그", "디버깅", "반복 오류", "재현", "실패 원인"),
    "hermes_runtime": ("반복 장애", "회귀", "퇴역", "live_verified", "증거 티켓"),
    "code-review": ("코드리뷰", "코드 리뷰", "코드 점검", "코드점검", "코드 품질", "코드품질"),
}


@dataclass(frozen=True)
class CapabilityResolution:
    capability_ids: tuple[str, ...]
    context: str
    omitted: tuple[str, ...]


def discover_skill_ids() -> tuple[str, ...]:
    if not SKILLS_ROOT.is_dir():
        return ()
    return tuple(sorted(path.parent.name for path in SKILLS_ROOT.glob("*/SKILL.md") if path.is_file()))


def select_skill_ids(prompt: str) -> tuple[str, ...]:
    lowered = (prompt or "").casefold()
    return tuple(skill for skill, triggers in SKILL_TRIGGERS.items() if any(trigger.casefold() in lowered for trigger in triggers))


def resolve(prompt: str, *, max_chars: int = 6000, include_core: bool = True) -> CapabilityResolution:
    if max_chars < 100:
        raise ValueError("max_chars must be at least 100")
    selected: list[str] = []
    if include_core and CORE_SKILL in discover_skill_ids():
        selected.append(CORE_SKILL)
    for skill in select_skill_ids(prompt):
        if skill in discover_skill_ids() and skill not in selected:
            selected.append(skill)
    sections: list[str] = []
    omitted: list[str] = []
    remaining = max_chars
    for skill in selected:
        path = SKILLS_ROOT / skill / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        block = f"\n[Edge Agent skill: {skill}]\n{text.strip()}\n"
        if remaining <= 0:
            omitted.append(skill)
            continue
        limit = min(remaining, 700) if skill == CORE_SKILL else remaining
        sections.append(block[:limit])
        remaining -= min(len(block), limit)
        if len(block) > limit:
            omitted.append(skill)
    return CapabilityResolution(tuple(selected), "".join(sections), tuple(omitted))


def prepare_provider_argv(provider: str, args: list[str], *, max_chars: int = 6000) -> list[str]:
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
    context = resolve(original, max_chars=max_chars).context
    if not context:
        return list(args)
    prepared = list(args)
    prepared[prompt_index] = f"{context}\n\n[공통 가능 기능 요청]\n{original}"
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
