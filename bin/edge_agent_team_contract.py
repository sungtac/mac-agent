"""Load the canonical provider-neutral Telegram team contract."""

from __future__ import annotations

import os
from pathlib import Path


TEAM_CONTRACT_PATH = Path(
    os.environ.get(
        "EDGE_AGENT_TEAM_CONTRACT",
        str(Path(__file__).resolve().parents[1] / "config" / "edge-agent-team-contract.md"),
    )
).expanduser().resolve()
MAX_TEAM_CONTRACT_CHARS = 9000


def render_team_contract() -> str:
    """Return the shared contract or fail closed if it is unavailable."""
    text = TEAM_CONTRACT_PATH.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"공통 팀 계약이 비어 있습니다: {TEAM_CONTRACT_PATH}")
    if len(text) > MAX_TEAM_CONTRACT_CHARS:
        raise RuntimeError(f"공통 팀 계약이 허용 길이를 초과했습니다: {TEAM_CONTRACT_PATH}")
    return f"[공통 Edge Agent Telegram 팀 계약]\n{text}"


__all__ = ["TEAM_CONTRACT_PATH", "render_team_contract"]
