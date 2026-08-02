#!/usr/bin/env python3
"""Deterministic, side-effect-free Phase 2 routing rules."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Iterable

from edge_agent_plan_gate import is_approval
from edge_agent_router_contract import (
    ExecutionMode,
    RiskLevel,
    RoleAssignment,
    RouterDecision,
    RouterInput,
    RouterRole,
    TaskType,
)
from edge_agent_session_contract import Provider


_PROVIDER_ALIASES = {
    "claude": Provider.CLAUDE,
    "클로드": Provider.CLAUDE,
    "codex": Provider.CODEX,
    "코덱스": Provider.CODEX,
    "콕스": Provider.CODEX,
    "antigravity": Provider.ANTIGRAVITY,
    "agy": Provider.ANTIGRAVITY,
    "안티그래비티": Provider.ANTIGRAVITY,
    "안티": Provider.ANTIGRAVITY,
    "gemma": Provider.GEMMA,
    "젬마": Provider.GEMMA,
}

_TASK_PATTERNS: tuple[tuple[TaskType, tuple[str, ...]], ...] = (
    (TaskType.OPERATIONS, ("서비스", "launchagent", "재시작", "배포", "운영 환경", "상태 확인")),
    (TaskType.CODING, ("코드", "코딩", "구현", "버그", "리팩터링", "함수", "스크립트")),
    (TaskType.COMPARISON, ("비교", "대조", "차이", "교차검증", "교차 검증")),
    (TaskType.EXTRACTION, ("추출", "표로 정리", "자료를 정리", "뽑아")),
    (TaskType.DOCUMENT, ("보고서", "제안서", "사업계획서", "문서", "공문")),
    (TaskType.REVIEW, ("검토", "리뷰", "감사", "검증", "점검")),
    (TaskType.IMPROVEMENT, ("개선", "보완", "부족한", "향상")),
    (TaskType.WRITING, ("작성", "써줘", "문장", "고쳐줘", "자연스럽게")),
    (TaskType.RESEARCH, ("조사", "리서치", "시장", "공고", "찾아", "출처")),
    (TaskType.EXPLANATION, ("무슨 뜻", "설명", "알려줘", "의미")),
)

_HIGH_RISK_WORDS = ("토큰", "secret", "비밀번호", "삭제", "커밋", "푸시", "배포", "설치", "migration", "마이그레이션")
_SENSITIVE_REVIEW_WORDS = ("계약", "법률", "재무", "보안", "개인정보")
_MUTATION_WORDS = ("수정", "변경", "추가", "삭제", "구현", "고쳐", "작성", "만들")
_TEAM_WORDS = ("각각", "여러", "다수", "독립", "교차", "비교", "검토까지", "분석해서", "통합", "누락")
_DELIBERATION_WORDS = ("논의", "토론", "각자 의견", "너희들이", "실현 가능한 대화", "회의")
_CLAUDE_FORBID = ("claude는 사용하지", "클로드는 사용하지", "claude 없이", "클로드 없이", "no claude")


def normalize_text(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def mentioned_providers(text: str) -> tuple[Provider, ...]:
    lowered = normalize_text(text).casefold()
    found: list[Provider] = []
    for alias, provider in _PROVIDER_ALIASES.items():
        if alias.casefold() in lowered and provider not in found:
            found.append(provider)
    return tuple(found)


def _provider_role_pairs(text: str) -> tuple[RoleAssignment, ...]:
    normalized = normalize_text(text).casefold()
    positioned: list[tuple[int, RoleAssignment]] = []
    role_words = (
        (RouterRole.SOURCE_EXTRACTOR, ("추출", "정리")),
        (RouterRole.RESEARCHER, ("조사", "리서치", "찾아")),
        (RouterRole.WRITER, ("작성", "쓰", "초안")),
        (RouterRole.IMPROVEMENT_WRITER, ("개선", "보완", "고쳐")),
        (RouterRole.IMPLEMENTER, ("구현", "수정", "코딩")),
        (RouterRole.REVIEWER, ("검토", "리뷰", "감사", "검증")),
        (RouterRole.INTEGRATOR, ("통합", "합쳐")),
    )
    aliases_by_length = sorted(_PROVIDER_ALIASES.items(), key=lambda item: len(item[0]), reverse=True)
    for alias, provider in aliases_by_length:
        for role, words in role_words:
            match = re.search(rf"{re.escape(alias.casefold())}[^.!?\n]{{0,24}}(?:{'|'.join(map(re.escape, words))})", normalized)
            if match:
                assignment = RoleAssignment(role=role, provider=provider)
                if not any(existing == assignment for _, existing in positioned):
                    positioned.append((match.start(), assignment))
                break
    positioned.sort(key=lambda item: item[0])
    return tuple(assignment for _, assignment in positioned)


def _task_type(text: str) -> TaskType:
    lowered = normalize_text(text).casefold()
    for task_type, words in _TASK_PATTERNS:
        if any(word.casefold() in lowered for word in words):
            return task_type
    return TaskType.EXPLANATION


def _risk_level(text: str, task_type: TaskType) -> RiskLevel:
    lowered = normalize_text(text).casefold()
    if any(word.casefold() in lowered for word in _HIGH_RISK_WORDS):
        return RiskLevel.HIGH
    if task_type in {TaskType.OPERATIONS} or any(word.casefold() in lowered for word in _SENSITIVE_REVIEW_WORDS):
        return RiskLevel.HIGH
    if any(word.casefold() in lowered for word in _MUTATION_WORDS) or task_type in {TaskType.CODING, TaskType.WRITING, TaskType.DOCUMENT, TaskType.IMPROVEMENT}:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _claude_forbidden(text: str) -> bool:
    lowered = normalize_text(text).casefold()
    return any(phrase.casefold() in lowered for phrase in _CLAUDE_FORBID)


def _default_roles(task_type: TaskType, mode: ExecutionMode, providers: tuple[Provider, ...]) -> tuple[RoleAssignment, ...]:
    selected = providers[0] if providers else None
    if selected is not None:
        role = {
            TaskType.RESEARCH: RouterRole.RESEARCHER,
            TaskType.EXTRACTION: RouterRole.SOURCE_EXTRACTOR,
            TaskType.REVIEW: RouterRole.REVIEWER,
            TaskType.CODING: RouterRole.IMPLEMENTER,
            TaskType.EXPLANATION: RouterRole.EXPLAINER,
        }.get(task_type, RouterRole.WRITER)
        return (RoleAssignment(role, selected),)
    if mode == ExecutionMode.TEAM:
        # Four independent first-pass roles plus a Claude integration node.
        # The coordinator is a logical control-plane role, not a fifth model.
        return (
            RoleAssignment(RouterRole.SOURCE_EXTRACTOR, Provider.GEMMA),
            RoleAssignment(RouterRole.RESEARCHER, Provider.ANTIGRAVITY),
            RoleAssignment(RouterRole.IMPLEMENTER, Provider.CODEX),
            RoleAssignment(
                RouterRole.INTEGRATOR,
                Provider.CLAUDE,
                ("source_extractor", "researcher", "implementer"),
            ),
        )
    if mode == ExecutionMode.SINGLE:
        defaults = {
            TaskType.RESEARCH: (RoleAssignment(RouterRole.RESEARCHER, Provider.ANTIGRAVITY),),
            TaskType.EXTRACTION: (RoleAssignment(RouterRole.SOURCE_EXTRACTOR, Provider.GEMMA),),
            TaskType.CODING: (RoleAssignment(RouterRole.IMPLEMENTER, Provider.CODEX),),
            TaskType.REVIEW: (RoleAssignment(RouterRole.REVIEWER, Provider.ANTIGRAVITY),),
            TaskType.EXPLANATION: (RoleAssignment(RouterRole.EXPLAINER, Provider.CODEX),),
        }
        return defaults.get(task_type, (RoleAssignment(RouterRole.WRITER, Provider.CODEX),))
    if mode == ExecutionMode.SMALL_TEAM:
        if task_type in {TaskType.RESEARCH, TaskType.EXTRACTION}:
            return (
                RoleAssignment(RouterRole.RESEARCHER, Provider.ANTIGRAVITY),
                RoleAssignment(RouterRole.REVIEWER, Provider.CODEX, ("researcher",)),
            )
        if task_type == TaskType.COMPARISON:
            return (
                RoleAssignment(RouterRole.SOURCE_EXTRACTOR, Provider.GEMMA),
                RoleAssignment(RouterRole.REVIEWER, Provider.ANTIGRAVITY, ("source_extractor",)),
            )
        return (
            RoleAssignment(RouterRole.WRITER if task_type != TaskType.CODING else RouterRole.IMPLEMENTER, Provider.CODEX),
            RoleAssignment(RouterRole.REVIEWER, Provider.ANTIGRAVITY, ("writer",)),
        )
    if task_type in {TaskType.RESEARCH, TaskType.COMPARISON, TaskType.DOCUMENT}:
        return (
            RoleAssignment(RouterRole.SOURCE_EXTRACTOR, Provider.GEMMA),
            RoleAssignment(RouterRole.REQUIREMENT_ANALYST, Provider.ANTIGRAVITY, ("source_extractor",)),
            RoleAssignment(RouterRole.WRITER, Provider.CODEX, ("requirement_analyst",)),
            RoleAssignment(RouterRole.REVIEWER, Provider.ANTIGRAVITY, ("writer",)),
            RoleAssignment(RouterRole.INTEGRATOR, Provider.CODEX, ("reviewer",)),
        )
    if task_type == TaskType.EXTRACTION:
        return (
            RoleAssignment(RouterRole.SOURCE_EXTRACTOR, Provider.GEMMA),
            RoleAssignment(RouterRole.REVIEWER, Provider.ANTIGRAVITY, ("source_extractor",)),
            RoleAssignment(RouterRole.INTEGRATOR, Provider.CODEX, ("reviewer",)),
        )
    return (
        RoleAssignment(RouterRole.WRITER, Provider.CODEX),
        RoleAssignment(RouterRole.REVIEWER, Provider.ANTIGRAVITY, ("writer",)),
        RoleAssignment(RouterRole.INTEGRATOR, Provider.CODEX, ("reviewer",)),
    )


def _execution_mode(text: str, task_type: TaskType, attachment_count: int, explicit_roles: tuple[RoleAssignment, ...]) -> ExecutionMode:
    if len(explicit_roles) >= 3 or attachment_count >= 3:
        return ExecutionMode.TEAM
    lowered = normalize_text(text).casefold()
    if any(word.casefold() in lowered for word in _DELIBERATION_WORDS):
        return ExecutionMode.TEAM
    review_plus_write = any(word in lowered for word in ("작성", "작성하고", "개선", "수정")) and any(word in lowered for word in ("검토", "리뷰", "검증"))
    team_signal = any(word.casefold() in lowered for word in _TEAM_WORDS)
    improvement_signal = any(word in lowered for word in ("개선", "보완", "부족한", "향상"))
    if len(explicit_roles) >= 2 or review_plus_write or (task_type in {TaskType.IMPROVEMENT, TaskType.DOCUMENT} and improvement_signal) or (team_signal and attachment_count >= 2):
        return ExecutionMode.SMALL_TEAM
    return ExecutionMode.SINGLE


def route(request: RouterInput | str, *, attachment_count: int | None = None) -> RouterDecision:
    if isinstance(request, str):
        request = RouterInput(request, attachment_count=attachment_count or 0)
    text = normalize_text(request.text)
    task_type = _task_type(text)
    explicit = tuple(request.explicit_provider for _ in [0]) if request.explicit_provider else mentioned_providers(text)
    explicit_roles = _provider_role_pairs(text)
    mode = _execution_mode(text, task_type, request.attachment_count, explicit_roles)
    claude_forbidden = _claude_forbidden(text)
    risk = _risk_level(text, task_type)
    roles = explicit_roles or _default_roles(task_type, mode, explicit)
    if explicit and not explicit_roles:
        roles = _default_roles(task_type, mode, explicit[:1])
    if explicit_roles and len(explicit_roles) == 1 and mode != ExecutionMode.SINGLE:
        roles = explicit_roles
    if claude_forbidden:
        roles = tuple(item for item in roles if item.provider != Provider.CLAUDE)
        if not roles:
            fallback_role = RouterRole.REVIEWER if task_type in {TaskType.REVIEW, TaskType.COMPARISON} else RouterRole.WRITER
            fallback_provider = Provider.ANTIGRAVITY if fallback_role == RouterRole.REVIEWER else Provider.CODEX
            roles = (RoleAssignment(fallback_role, fallback_provider),)
    claude_required = (
        not claude_forbidden
        and any(word.casefold() in text.casefold() for word in _SENSITIVE_REVIEW_WORDS)
    )
    if claude_required and all(item.provider != Provider.CLAUDE for item in roles) and mode != ExecutionMode.SINGLE:
        roles = roles + (RoleAssignment(RouterRole.REVIEWER, Provider.CLAUDE),)
    if claude_required and mode == ExecutionMode.SINGLE:
        roles = (RoleAssignment(RouterRole.REVIEWER, Provider.CLAUDE),)
    if len(roles) >= 3:
        mode = ExecutionMode.TEAM
    elif len(roles) == 2:
        mode = ExecutionMode.SMALL_TEAM
    reasons = [f"task_type:{task_type.value}", f"risk:{risk.value}"]
    if explicit:
        reasons.append("explicit_provider")
    if explicit_roles:
        reasons.append("explicit_role_order")
    if request.attachment_count:
        reasons.append("attachments")
    if claude_forbidden:
        reasons.append("claude_forbidden")
    if is_approval(text):
        reasons.append("approval_message")
    return RouterDecision(
        task_type=task_type,
        risk_level=risk,
        execution_mode=mode,
        roles=roles,
        claude_required=claude_required,
        claude_forbidden=claude_forbidden,
        requires_approval=risk in {RiskLevel.MEDIUM, RiskLevel.HIGH},
        requires_worktree=task_type == TaskType.CODING or any(word in text.casefold() for word in _MUTATION_WORDS),
        reason_codes=tuple(reasons),
        normalized_text=text,
    )


def route_many(requests: Iterable[RouterInput]) -> tuple[RouterDecision, ...]:
    return tuple(route(item) for item in requests)
