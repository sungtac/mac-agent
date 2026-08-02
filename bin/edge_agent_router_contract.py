#!/usr/bin/env python3
"""Small, side-effect-free contracts for the Phase 2 natural-language router."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from edge_agent_session_contract import Provider


class TaskType(StrEnum):
    EXPLANATION = "explanation"
    WRITING = "writing"
    RESEARCH = "research"
    DOCUMENT = "document"
    CODING = "coding"
    REVIEW = "review"
    COMPARISON = "comparison"
    EXTRACTION = "extraction"
    OPERATIONS = "operations"
    IMPROVEMENT = "improvement"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExecutionMode(StrEnum):
    SINGLE = "single"
    SMALL_TEAM = "small_team"
    TEAM = "team"


class RouterRole(StrEnum):
    SOURCE_EXTRACTOR = "source_extractor"
    RESEARCHER = "researcher"
    REQUIREMENT_ANALYST = "requirement_analyst"
    COMPANY_FIT_ANALYST = "company_fit_analyst"
    WRITER = "writer"
    IMPROVEMENT_WRITER = "improvement_writer"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    INTEGRATOR = "integrator"
    EXPLAINER = "explainer"


def _bounded_text(name: str, value: object, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    if len(text) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    return text


@dataclass(frozen=True)
class RouterInput:
    text: str
    attachment_count: int = 0
    explicit_provider: Provider | str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _bounded_text("text", self.text, 4000))
        if not isinstance(self.attachment_count, int) or self.attachment_count < 0 or self.attachment_count > 100:
            raise ValueError("attachment_count must be an integer from 0 to 100")
        if self.explicit_provider is not None:
            object.__setattr__(self, "explicit_provider", Provider(self.explicit_provider))
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")


@dataclass(frozen=True)
class RoleAssignment:
    role: RouterRole | str
    provider: Provider | str
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", RouterRole(self.role))
        object.__setattr__(self, "provider", Provider(self.provider))
        dependencies = tuple(str(item).strip() for item in self.dependencies if str(item).strip())
        if len(dependencies) > 5:
            raise ValueError("a role cannot have more than 5 dependencies")
        object.__setattr__(self, "dependencies", dependencies)


@dataclass(frozen=True)
class RouterDecision:
    task_type: TaskType | str
    risk_level: RiskLevel | str
    execution_mode: ExecutionMode | str
    roles: tuple[RoleAssignment, ...]
    claude_required: bool = False
    claude_forbidden: bool = False
    requires_approval: bool = False
    requires_worktree: bool = False
    reason_codes: tuple[str, ...] = ()
    normalized_text: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_type", TaskType(self.task_type))
        object.__setattr__(self, "risk_level", RiskLevel(self.risk_level))
        object.__setattr__(self, "execution_mode", ExecutionMode(self.execution_mode))
        roles = tuple(self.roles)
        if not roles or len(roles) > 6:
            raise ValueError("roles must contain 1 to 6 assignments")
        if len({item.provider for item in roles}) > 3:
            raise ValueError("a plan may use at most 3 providers")
        if self.claude_required and self.claude_forbidden:
            raise ValueError("claude cannot be both required and forbidden")
        object.__setattr__(self, "roles", roles)
        object.__setattr__(self, "reason_codes", tuple(str(item).strip() for item in self.reason_codes if str(item).strip()))
        if self.normalized_text:
            object.__setattr__(self, "normalized_text", _bounded_text("normalized_text", self.normalized_text, 4000))

    @property
    def providers(self) -> tuple[Provider, ...]:
        return tuple(dict.fromkeys(item.provider for item in self.roles))

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type.value,
            "risk_level": self.risk_level.value,
            "execution_mode": self.execution_mode.value,
            "roles": [
                {"role": item.role.value, "provider": item.provider.value, "dependencies": list(item.dependencies)}
                for item in self.roles
            ],
            "claude_required": self.claude_required,
            "claude_forbidden": self.claude_forbidden,
            "requires_approval": self.requires_approval,
            "requires_worktree": self.requires_worktree,
            "reason_codes": list(self.reason_codes),
            "normalized_text": self.normalized_text,
        }
