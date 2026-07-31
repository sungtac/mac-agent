#!/usr/bin/env python3
"""Ponytail-inspired minimality review; it never edits code automatically."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MinimalityMode(StrEnum):
    OFF = "off"
    LITE = "lite"
    FULL = "full"
    ULTRA = "ultra"


@dataclass(frozen=True)
class MinimalityReview:
    mode: MinimalityMode
    applicable: bool
    questions: tuple[str, ...]
    protected_guards: tuple[str, ...]
    reason: str


PROTECTED_GUARDS = (
    "입력 검증",
    "권한·보안 경계",
    "예외 처리",
    "테스트와 검증",
    "롤백·감사 기록",
)


def review_for(*, mode: MinimalityMode | str, sensitive_path: bool = False, task_kind: str = "code") -> MinimalityReview:
    selected = MinimalityMode(mode)
    if selected == MinimalityMode.OFF:
        return MinimalityReview(selected, False, (), PROTECTED_GUARDS, "minimality review disabled")
    if sensitive_path:
        return MinimalityReview(selected, False, (), PROTECTED_GUARDS, "sensitive path requires full safety review")
    if task_kind in {"ops", "delete", "send", "system"}:
        return MinimalityReview(selected, False, (), PROTECTED_GUARDS, "operational or external action is outside minimality review")
    questions = (
        "이 기능이 정말 새로 필요한가?",
        "기존 코드베이스의 helper·패턴을 재사용할 수 있는가?",
        "표준 라이브러리나 플랫폼 기능으로 해결할 수 있는가?",
        "이미 설치된 dependency로 충분한가?",
        "요구사항을 만족하는 최소 diff인가?",
    )
    if selected == MinimalityMode.ULTRA:
        questions += ("이 요구사항 중 아직 검증되지 않은 speculative 부분은 무엇인가?",)
    return MinimalityReview(selected, True, questions, PROTECTED_GUARDS, "review-only minimality gate")
