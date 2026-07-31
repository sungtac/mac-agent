"""Provider-neutral paths for portable Edge Agent skills.

Skills must not derive persistent state from an OpenClaw workspace.  The
runtime root is configurable for tests and installations, but defaults to the
shared Edge Agent state directory.
"""

from __future__ import annotations

import os
from pathlib import Path


def runtime_root() -> Path:
    return Path(os.environ.get("EDGE_AGENT_RUNTIME_ROOT", "~/.edge-agent")).expanduser().resolve()


def state_root() -> Path:
    return Path(os.environ.get("EDGE_AGENT_STATE_ROOT", str(runtime_root() / "state"))).expanduser().resolve()


def skill_state_path(skill_name: str) -> Path:
    safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in skill_name).strip("._")
    if not safe_name:
        raise ValueError("skill_name must contain at least one safe character")
    return state_root() / "skills" / f"{safe_name}.json"
