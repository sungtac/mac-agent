#!/usr/bin/env python3
"""Load and render the shared Edge Agent identity and persona contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "config" / "agent-profile-contract.json"
ALIASES = {"agy": "antigravity", "gemini": "antigravity", "gemma": "roda"}


def load_profile_contract() -> dict[str, Any]:
    data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != "edge_agent.agent_profile.v1":
        raise ValueError("지원하지 않는 agent profile schema")
    if not data.get("version"):
        raise ValueError("agent profile 계약의 필수 필드가 없음")
    style = data.get("common_style")
    agents = data.get("agents")
    if not isinstance(style, dict) or not isinstance(agents, dict):
        raise ValueError("agent profile 계약의 공통 규칙 또는 agents 구조가 잘못됨")
    if not isinstance(style.get("id"), str) or not isinstance(style.get("audience"), str):
        raise ValueError("agent profile 계약의 공통 스타일 메타데이터가 잘못됨")
    if not isinstance(style.get("rules"), list) or not all(isinstance(rule, str) for rule in style["rules"]):
        raise ValueError("agent profile 계약의 공통 규칙이 잘못됨")
    if not isinstance(style.get("forbidden_formatting"), list) or not all(
        isinstance(item, str) for item in style["forbidden_formatting"]
    ):
        raise ValueError("agent profile 계약의 금지 문법 목록이 잘못됨")
    for role in ("claude", "codex", "antigravity", "roda"):
        agent = agents.get(role)
        if not isinstance(agent, dict):
            raise ValueError(f"agent profile 계약에 {role} 역할이 없음")
        text_fields = ("label", "identity", "mission", "default_persona")
        if any(not isinstance(agent.get(field), str) for field in text_fields):
            raise ValueError(f"agent profile 계약의 {role} 필드가 잘못됨")
        if not isinstance(agent["phase_personas"], dict) or not isinstance(agent["personas"], dict):
            raise ValueError(f"agent profile 계약의 {role} persona 구조가 잘못됨")
        if any(not isinstance(persona, str) for persona in agent["phase_personas"].values()):
            raise ValueError(f"agent profile 계약의 {role} phase persona가 잘못됨")
        if agent["default_persona"] not in agent["personas"]:
            raise ValueError(f"agent profile 계약의 {role} 기본 persona가 없음")
        for persona_name, persona in agent["personas"].items():
            if not isinstance(persona_name, str) or not isinstance(persona, dict):
                raise ValueError(f"agent profile 계약의 {role} persona가 잘못됨")
            if not isinstance(persona.get("label"), str) or not isinstance(persona.get("instruction"), str):
                raise ValueError(f"agent profile 계약의 {role}/{persona_name} 필드가 잘못됨")
        if any(persona not in agent["personas"] for persona in agent["phase_personas"].values()):
            raise ValueError(f"agent profile 계약의 {role} phase persona가 없음")
    return data


def normalize_role(role: str) -> str:
    normalized = str(role or "").strip().lower()
    normalized = ALIASES.get(normalized, normalized)
    if normalized not in {"claude", "codex", "antigravity", "roda"}:
        raise ValueError(f"지원하지 않는 agent role: {role}")
    return normalized


def resolve_persona(role: str, persona: str | None = None, phase: str | None = None) -> str:
    contract = load_profile_contract()
    normalized_role = normalize_role(role)
    agent = contract["agents"][normalized_role]
    selected = persona or agent.get("phase_personas", {}).get(phase or "") or agent["default_persona"]
    if selected not in agent.get("personas", {}):
        raise ValueError(f"{normalized_role}에 없는 persona: {selected}")
    return selected


def render_agent_profile(role: str, persona: str | None = None, phase: str | None = None) -> str:
    contract = load_profile_contract()
    normalized_role = normalize_role(role)
    agent = contract["agents"][normalized_role]
    selected = resolve_persona(normalized_role, persona, phase)
    persona_data = agent["personas"][selected]
    style = contract["common_style"]
    rules = "\n".join(f"- {rule}" for rule in style["rules"])
    forbidden = ", ".join(style["forbidden_formatting"])
    return (
        f"[공통 답변 규칙: {style['id']}]\n"
        f"대상 독자: {style['audience']}\n"
        f"{rules}\n"
        f"사용하지 않을 장식 문법: {forbidden}\n\n"
        f"[영구 아이덴티티: {agent['label']}]\n"
        f"역할: {agent['identity']}\n"
        f"임무: {agent['mission']}\n\n"
        f"[현재 persona: {selected} / {persona_data['label']}]\n"
        f"{persona_data['instruction']}"
    )


def profile_metadata(role: str, persona: str | None = None, phase: str | None = None) -> dict[str, str]:
    contract = load_profile_contract()
    normalized_role = normalize_role(role)
    return {
        "role": normalized_role,
        "persona": resolve_persona(normalized_role, persona, phase),
        "style_version": contract["common_style"]["id"],
        "identity_version": contract["version"],
    }
