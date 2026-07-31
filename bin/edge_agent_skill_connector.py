"""Small, read-only connector from provider prompts to portable skill contracts."""

from __future__ import annotations

from pathlib import Path


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
CORE_SKILL = "edge-agent-behavior"
TRIGGERS = {
    "quota_resume": ("quota", "rate_limit", "429", "토큰 한도", "재개", "fallback"),
    "product_research": ("추천제품", "제품 추천", "최저가", "가성비", "쿠폰", "가격비교", "구매 링크"),
    "calendar": ("일정", "캘린더", "calendar", "회의 일정", "약속"),
    "roda_public_search": ("공개자료", "공개 검색", "출처", "논문 찾아", "사이트 찾아"),
    "harness-memory": ("디버그", "디버깅", "반복 오류", "재현", "실패 원인"),
    "hermes_runtime": ("반복 장애", "회귀", "퇴역", "live_verified", "증거 티켓"),
}


def select_skill_ids(prompt: str) -> list[str]:
    lowered = prompt.casefold()
    return [skill for skill, triggers in TRIGGERS.items() if any(t.casefold() in lowered for t in triggers)]


def build_skill_context(prompt: str, *, max_chars: int = 6000) -> str:
    selected = [CORE_SKILL, *select_skill_ids(prompt)]
    sections: list[str] = []
    remaining = max_chars
    for skill in selected:
        path = SKILLS_ROOT / skill / "SKILL.md"
        if not path.is_file() or remaining <= 0:
            continue
        text = path.read_text(encoding="utf-8")
        chunk = f"\n[Edge Agent skill: {skill}]\n{text}\n"
        # Reserve room for request-specific skills even when callers use a
        # small prompt budget. The core contract is intentionally concise.
        limit = min(remaining, 700) if skill == CORE_SKILL else remaining
        sections.append(chunk[:limit])
        remaining -= min(len(chunk), limit)
    return "".join(sections)
